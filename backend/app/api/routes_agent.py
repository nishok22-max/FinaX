"""Agent-layer control API (FR-18…FR-22).

Import-light by design: nothing at module scope touches the optional agent stack, so
``app.main`` can mount this router unconditionally and every route self-gates to a clean 503
when the layer is off. See ``tests/test_agent_isolation.py``.

**The approval route is the only place in the agent layer where a transaction can happen**, and
it does so by calling ``ProtectionService.protect`` — the identical call
``POST /positions/{borrower}/protect`` makes, with the borrower's stored, signed mandate replayed
verbatim. The breaker, the in-flight lock, the sizing math, the simulator, the submitter and the
vault's own on-chain checks all run exactly as they do for a manual request. The agent contributes
a recommendation and a checklist; it contributes nothing to the execution path.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.agent import policy, tools
from app.agent.models import (
    AgentStatusResponse,
    ApprovalRequest,
    ApprovalResult,
    AuditAction,
    AuditRow,
    ChatHistory,
    ChatReply,
    ChatRequest,
    CrewRunRequest,
    CrewRunResult,
    PolicyDecision,
    ProposalRow,
    ProposalStatus,
    RejectRequest,
    TuningRow,
    TuningStatus,
)
from app.agent.runtime import AgentRuntime, agent_status, get_runtime
from app.core.breaker import TripReason
from app.core.models import RiskParams
from app.core.protection_service import ProtectionService
from app.deps import get_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

Service = Annotated[ProtectionService, Depends(get_service)]

_HOUR = 3600.0


def require_runtime() -> AgentRuntime:
    """Every route but ``/agent/status`` needs a live runtime; refuse with the actual reason."""
    runtime = get_runtime()
    if runtime is None:
        raise HTTPException(503, detail=agent_status().reason or "agent layer disabled")
    return runtime


Runtime = Annotated[AgentRuntime, Depends(require_runtime)]


# --- Status ------------------------------------------------------------------------------------


@router.get("/status")
async def status() -> AgentStatusResponse:
    """Always 200, even when the layer is off — the console polls this to decide what to render,
    and an error here would present as a broken page rather than a disabled feature."""
    st = agent_status()
    pending = 0
    runtime = get_runtime()
    if runtime is not None:
        pending = len(await runtime.store.list_proposals(
            status=ProposalStatus.PENDING, limit=1000
        ))
    return AgentStatusResponse(
        enabled=st.enabled, reason=st.reason, model=st.model,
        stack_available=st.stack_available, store_ready=st.store_ready,
        pending_proposals=pending,
    )


# --- Chat --------------------------------------------------------------------------------------


@router.post("/chat")
async def chat(body: ChatRequest, runtime: Runtime, service: Service) -> ChatReply:
    """Answer an operator question using the read-only tools against live keeper state."""
    from app.agent.chat import run_chat  # heavy: imported per request, never at module scope

    thread_id = body.thread_id or uuid.uuid4().hex
    return await run_chat(
        runtime=runtime, service=service, thread_id=thread_id,
        message=body.message, borrower=body.borrower,
    )


@router.get("/chat/{thread_id}/history")
async def chat_history(thread_id: str, runtime: Runtime, limit: int = 50) -> ChatHistory:
    return ChatHistory(
        thread_id=thread_id, messages=await runtime.store.history(thread_id, limit=limit)
    )


@router.delete("/chat/{thread_id}")
async def delete_chat(thread_id: str, runtime: Runtime) -> dict[str, bool]:
    return {"deleted": await runtime.store.delete_thread(thread_id)}


# --- Crew --------------------------------------------------------------------------------------


@router.post("/crew/run")
async def crew_run(body: CrewRunRequest, runtime: Runtime, service: Service) -> CrewRunResult:
    """Run the crew for one borrower. Produces a proposal at most — never a transaction."""
    from app.agent.graph import run_crew  # heavy: imported per request

    return await run_crew(
        runtime=runtime, service=service, borrower=body.borrower, trigger=body.trigger
    )


# --- Proposals ---------------------------------------------------------------------------------


@router.get("/proposals")
async def list_proposals(
    runtime: Runtime,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    borrower: str | None = None,
    limit: int = 20,
) -> list[ProposalRow]:
    parsed: ProposalStatus | None = None
    if status_filter is not None:
        try:
            parsed = ProposalStatus(status_filter.upper())
        except ValueError as exc:
            raise HTTPException(400, detail=f"unknown status {status_filter!r}") from exc
    return await runtime.store.list_proposals(status=parsed, borrower=borrower, limit=limit)


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: int, runtime: Runtime) -> ProposalRow:
    row = await runtime.store.get_proposal(proposal_id)
    if row is None:
        raise HTTPException(404, detail=f"proposal {proposal_id} not found")
    return row


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: int, body: RejectRequest, runtime: Runtime
) -> ProposalRow:
    row = await runtime.store.get_proposal(proposal_id)
    if row is None:
        raise HTTPException(404, detail=f"proposal {proposal_id} not found")
    if not await runtime.store.claim_proposal(proposal_id, status=ProposalStatus.REJECTED):
        raise HTTPException(409, detail=f"proposal is {row.status.value}, not PENDING")
    await runtime.store.finish_proposal(
        proposal_id, status=ProposalStatus.REJECTED,
        decided_by=body.rejected_by, note=body.note,
    )
    await runtime.store.audit(
        actor="human", action=AuditAction.REJECTED, borrower=row.borrower,
        proposal_id=proposal_id, detail={"by": body.rejected_by, "note": body.note},
    )
    refreshed = await runtime.store.get_proposal(proposal_id)
    assert refreshed is not None
    return refreshed


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: int, body: ApprovalRequest, runtime: Runtime, service: Service
) -> ApprovalResult:
    """Approve and execute a proposal.

    The order below is the safety argument, and each step exists because skipping it would be a
    real failure:

    1. The proposal must still be ``PENDING``.
    2. The borrower must still have a signed mandate — it can be replaced between propose and
       approve, and the stored proposal was reasoned about the old one.
    3. The position is **re-read and re-assessed**. The figures in the proposal describe a moment
       that has passed; a market moves while a human reads.
    4. The gate runs **again**, on those fresh figures. A proposal that no longer qualifies is
       marked ``STALE`` and refused, not executed on figures that no longer hold.
    5. The TTL is checked, separately from the gate, so "nobody looked in time" is distinguishable
       from "the position moved".
    6. The status is claimed atomically, so two concurrent approvals cannot both proceed.
    7. Only then does ``service.protect`` run — the same call the manual route makes.
    """
    store = runtime.store
    row = await store.get_proposal(proposal_id)
    if row is None:
        raise HTTPException(404, detail=f"proposal {proposal_id} not found")
    if row.status is not ProposalStatus.PENDING:
        raise HTTPException(409, detail=f"proposal is {row.status.value}, not PENDING")

    registered = service.params_of(row.borrower)
    if registered is None:
        await store.finish_proposal(proposal_id, status=ProposalStatus.STALE,
                                    decided_by=body.approved_by)
        raise HTTPException(409, detail="borrower no longer has a registered signed mandate")
    params, signature = registered

    gate = await revalidate(runtime, service, row.borrower, params)
    if not gate.allowed:
        await store.finish_proposal(proposal_id, status=ProposalStatus.STALE,
                                    decided_by=body.approved_by, note=body.note)
        await store.audit(
            actor="system", action=AuditAction.STALE_ON_APPROVE, borrower=row.borrower,
            proposal_id=proposal_id, detail={"blocking": gate.blocking},
        )
        raise HTTPException(
            409,
            detail={"error": "the position no longer satisfies the policy gate",
                    "blocking": gate.blocking,
                    "checks": [c.model_dump() for c in gate.checks if not c.passed]},
        )

    now = time.time()
    if now > row.expires_at and not body.acknowledge_stale:
        await store.finish_proposal(proposal_id, status=ProposalStatus.EXPIRED,
                                    decided_by=body.approved_by)
        await store.audit(actor="system", action=AuditAction.EXPIRED_ON_APPROVE,
                          borrower=row.borrower, proposal_id=proposal_id, detail={})
        raise HTTPException(
            409, detail="proposal expired; re-run the crew, or approve with acknowledge_stale"
        )

    # Atomic claim: exactly one concurrent approver may proceed past this line.
    if not await store.claim_proposal(proposal_id, status=ProposalStatus.APPROVED):
        raise HTTPException(409, detail="proposal was already decided by someone else")
    await store.finish_proposal(proposal_id, status=ProposalStatus.APPROVED,
                                decided_by=body.approved_by, note=body.note)
    await store.audit(actor="human", action=AuditAction.APPROVED, borrower=row.borrower,
                      proposal_id=proposal_id, detail={"by": body.approved_by})

    try:
        # The identical call POST /positions/{borrower}/protect makes, on the stored signed pair.
        result = await service.protect(params, signature)
    except Exception as exc:  # record the failure, never leak a traceback to a client
        logger.exception("approved proposal %d failed to execute", proposal_id)
        await store.finish_proposal(proposal_id, status=ProposalStatus.FAILED,
                                    note=f"execution error: {exc}")
        await store.audit(actor="system", action=AuditAction.FAILED, borrower=row.borrower,
                          proposal_id=proposal_id, detail={"error": str(exc)})
        raise HTTPException(502, detail=f"execution failed: {exc}") from exc

    final = ProposalStatus.EXECUTED if result.submitted else ProposalStatus.FAILED
    await store.finish_proposal(
        proposal_id, status=final, tx_hash=result.tx_hash, result=result.model_dump(mode="json"),
    )
    await store.audit(
        actor="system",
        action=AuditAction.EXECUTED if result.submitted else AuditAction.FAILED,
        borrower=row.borrower, proposal_id=proposal_id,
        detail={"tx_hash": result.tx_hash, "state": result.state.value,
                "reason": result.reason},
    )

    refreshed = await store.get_proposal(proposal_id)
    assert refreshed is not None
    return ApprovalResult(
        proposal=refreshed, revalidated_gate=gate, protect=result, reason=result.reason
    )


async def revalidate(
    runtime: AgentRuntime, service: ProtectionService, borrower: str, params: RiskParams
) -> PolicyDecision:
    """Re-run the gate against a freshly read position (FR-19).

    Shared by the approve route and the crew graph so both judge a proposal by the same rules —
    a second, drifting copy of the gate's inputs would be exactly the bug the gate exists to
    prevent.
    """
    store = runtime.store
    assessment = await service.assess(params, sigma=tools.sigma_for(service, params))
    snapshot = await service.snapshot(borrower, params.hf_trigger_bps)
    debt_asset, debt_decimals = tools.debt_asset_facts()
    debt_price = await _debt_price_base(service, debt_asset)
    since = time.time() - _HOUR
    return policy.evaluate(
        params=params,
        assessment=assessment,
        snapshot=snapshot,
        metrics=service.metrics(),
        limits=runtime.limits(),
        now=time.time(),
        debt_price_base=debt_price,
        debt_decimals=debt_decimals,
        recent_proposals_borrower=await store.count_recent_proposals(
            since=since, borrower=borrower
        ),
        recent_proposals_global=await store.count_recent_proposals(since=since),
    )


async def _debt_price_base(service: ProtectionService, debt_asset: str) -> int:
    """Oracle price of the debt asset in Aave base units, used to value the repay bound.

    Falls back to 0 on any read failure, which fails the ``repay_bounded`` check closed: an
    unvalued repay must block rather than pass by arithmetic accident.
    """
    try:
        from app.chain.oracle import OracleClient

        return (await OracleClient().get_asset_price(debt_asset)).price
    except Exception:  # an unreadable oracle blocks the gate rather than 500-ing
        logger.warning("could not read the debt-asset price; repay bound will fail closed",
                       exc_info=True)
        return 0


# --- Tuning suggestions (re-sign requests, FR-21) -----------------------------------------------


@router.get("/tuning")
async def list_tuning(
    runtime: Runtime, borrower: str | None = None, include_closed: bool = False, limit: int = 20
) -> list[TuningRow]:
    return await runtime.store.list_tuning(
        borrower=borrower, status=None if include_closed else TuningStatus.OPEN, limit=limit
    )


@router.post("/tuning/{tuning_id}/dismiss")
async def dismiss_tuning(tuning_id: int, runtime: Runtime) -> TuningRow:
    row = await runtime.store.get_tuning(tuning_id)
    if row is None:
        raise HTTPException(404, detail=f"tuning suggestion {tuning_id} not found")
    if not await runtime.store.set_tuning_status(tuning_id, TuningStatus.DISMISSED):
        raise HTTPException(409, detail=f"suggestion is {row.status.value}, not open")
    await runtime.store.audit(actor="human", action=AuditAction.TUNING_DISMISSED,
                              borrower=row.borrower, detail={"tuning_id": tuning_id})
    refreshed = await runtime.store.get_tuning(tuning_id)
    assert refreshed is not None
    return refreshed


# --- Audit & kill switch ------------------------------------------------------------------------


@router.get("/audit")
async def audit_trail(
    runtime: Runtime, borrower: str | None = None, limit: int = 50
) -> list[AuditRow]:
    return await runtime.store.list_audit(borrower=borrower, limit=limit)


@router.post("/panic")
async def panic(
    runtime: Runtime,
    service: Service,
    reason: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Stop everything: trip the circuit breaker manually.

    ``CircuitBreaker.trip`` and ``TripReason.MANUAL`` have existed since Phase 5 and nothing has
    ever called them. Wiring the button here makes "stop the agent now" a real, immediate action
    rather than a config change and a restart — and because it trips the *keeper's* breaker, it
    halts autonomous submission too, not just the agent. ``POST /breaker/reset`` undoes it.
    """
    service.breaker.trip(TripReason.MANUAL)
    await runtime.store.audit(actor="human", action=AuditAction.PANIC,
                              detail={"reason": reason or "operator panic"})
    return {
        "paused": service.breaker.paused,
        "trip_reason": service.breaker.trip_reason,
        "note": "autonomous submission is halted; POST /breaker/reset to resume",
    }
