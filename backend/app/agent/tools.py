"""Tool bodies — what the agents are allowed to do, expressed as plain Python.

Two properties are load-bearing:

**No LangChain types appear here.** Every tool is an ordinary function taking its dependencies
explicitly, so the whole surface is unit-testable without an LLM, without a network, and without
the optional agent stack installed. :mod:`app.agent.toolspec` adapts these into ``StructuredTool``s
for the model; that adapter is the only file that needs the third-party import.

**There is no submission tool.** Not a disabled one, not a gated one — none. The agent cannot
build calldata, cannot reach :class:`~app.core.submitter.Submitter`, and cannot call
``ProtectionService.protect``. Execution is reachable only through the human-approved HTTP route
in ``app/api/routes_agent.py``, which replays the borrower's stored signed mandate through the
same path a manual request takes. That makes "the agent cannot submit" a structural fact about
the code rather than a behavioural promise about the model.

The two write tools here write *proposals*, never state: a row in SQLite that a human must act on.
"""
from __future__ import annotations

from typing import Any

from app.agent.models import PolicyDecision, ProposalStatus, TunableField
from app.agent.store import AgentStore
from app.config.arbitrum import TOKEN_DECIMALS, TOKEN_SYMBOLS, TOKENS
from app.core.models import RiskParams, RiskSignal
from app.core.protection_service import ProtectionService
from app.core.risk import assess_risk
from app.core.state import VALID_TRANSITIONS, PositionState

#: Default trigger used when a borrower is not registered, matching routes_positions.py so a
#: snapshot read through the agent agrees with one read through the REST API.
_DEFAULT_TRIGGER_BPS = 11_500

#: The vault's custom errors, in the vault's own vocabulary. A simulation or a revert names one of
#: these and nothing else, so an explanation layer needs no guesswork — and the model is given the
#: authoritative text rather than inventing a plausible-sounding one.
_VAULT_ERRORS: dict[str, str] = {
    "NotAuthorized": "The caller is not the configured keeper. Only the keeper address set on "
                     "the vault may invoke executeProtection.",
    "BadSignature": "The EIP-712 signature does not recover to the borrower in the RiskParams. "
                    "The mandate was not signed by its owner, or a field was altered after "
                    "signing — any change to RiskParams requires a fresh signature.",
    "NonceUsed": "This mandate's nonce has already been consumed. Each signed RiskParams is "
                 "single-use; the borrower must sign a new one.",
    "Expired": "The mandate's deadline has passed. The borrower must re-sign with a later "
               "deadline.",
    "CollateralNotAllowed": "The chosen collateral is not in the borrower's signed allow-list. "
                            "The vault will only touch assets the borrower explicitly permitted.",
    "NoDebt": "The position carries no debt, so there is nothing to repay.",
    "TargetOutOfBand": "The requested health-factor target falls outside the band the borrower "
                       "signed (hfTargetBaseBps..hfTargetMaxBps).",
    "CallerNotPool": "executeOperation was invoked by something other than the Aave pool. The "
                     "flash-loan callback only accepts the pool.",
    "BadInitiator": "The flash loan was initiated by an address other than this vault.",
    "AssetMismatch": "The asset returned in the flash-loan callback is not the debt asset the "
                     "operation was built for.",
    "CostExceeded": "The intervention's total cost exceeded the borrower's signed maxCostBps. "
                    "The vault refuses a rescue that costs more than the mandate permits.",
    "HealthBelowTarget": "After the repay and swap, the health factor still sits below the "
                         "target — the sized repay was insufficient, so the whole transaction "
                         "reverted and the position is unchanged.",
    "DebtNotReduced": "The operation did not reduce the borrower's debt. The vault enforces that "
                      "a rescue makes the position strictly better.",
    "LeverageIncreased": "The operation would have increased leverage. The vault refuses any "
                         "path that leaves the borrower more exposed than before.",
}

_STATE_EXPLANATIONS: dict[PositionState, str] = {
    PositionState.HEALTHY: "Health factor is comfortably above the trigger; nothing to do.",
    PositionState.WATCH: "Health factor is inside the watch band above the trigger — close "
                         "enough to reassess each poll, not yet close enough to act.",
    PositionState.ASSESSING: "Health factor is at or below the trigger. The decision pipeline is "
                             "sizing a repay, selecting collateral, and testing viability.",
    PositionState.DECLINED: "The pipeline evaluated an intervention and declined it — typically "
                            "because the cost exceeded the value protected, or no eligible "
                            "collateral had the required aToken allowance. Transient: the "
                            "position returns to WATCH and is reconsidered.",
    PositionState.READY: "A rescue simulated successfully and is about to be submitted.",
    PositionState.SUBMITTED: "A rescue transaction is broadcast and awaiting its receipt. The "
                             "in-flight lock prevents a second submission for this borrower.",
    PositionState.RESTORED: "The rescue executed and the health factor reached its target.",
    PositionState.REVERTED: "The rescue reverted atomically. No partial state was written and "
                            "the position is exactly as it was.",
}

_DOCTRINE = (
    "FinaX protects Aave V3 positions from liquidation. Its operating doctrine is "
    "'math proposes, simulation validates, Solidity enforces': a closed-form formula sizes the "
    "minimum repay that restores the health factor to a volatility-adaptive target, an eth_call "
    "simulation proves the transaction would succeed, and the LiquidationShieldVault re-verifies "
    "the borrower's EIP-712 mandate and every safety invariant on chain before anything settles. "
    "The agent layer orchestrates and explains; it is not the decision-maker. Every figure it "
    "reports is read back from the backend's own response models, and no action reaches the "
    "chain without a human approving it."
)


# --- Read-only tools -------------------------------------------------------------------------


async def t_doctrine() -> str:
    """What this system is and where the agent's authority ends."""
    return _DOCTRINE


async def t_list_positions(service: ProtectionService) -> list[str]:
    """Borrower addresses with a registered signed mandate.

    The REST API has no list endpoint; the registry is only visible as a count on ``/metrics``.
    """
    return service.registered()


async def t_position_state(service: ProtectionService, borrower: str) -> dict[str, str]:
    """The borrower's lifecycle state, with its meaning. No chain read."""
    state = service.state_of(borrower)
    return {"borrower": borrower, "state": state.value,
            "meaning": _STATE_EXPLANATIONS.get(state, "Unknown state.")}


async def t_position_snapshot(service: ProtectionService, borrower: str) -> dict[str, Any]:
    """Live account data: health factor, collateral, debt, and lifecycle state."""
    registered = service.params_of(borrower)
    trigger = registered[0].hf_trigger_bps if registered else _DEFAULT_TRIGGER_BPS
    snap = await service.snapshot(borrower, trigger)
    return {
        "borrower": snap.borrower,
        "state": snap.state.value,
        "hf": snap.hf,
        "hf_trigger_bps": snap.hf_trigger_bps,
        "collateral_usd": snap.account.collateral_usd,
        "debt_usd": snap.account.debt_usd,
        "liquidation_threshold_bps": snap.account.liquidation_threshold_bps,
        "has_debt": snap.account.has_debt,
        "registered": registered is not None,
    }


async def t_registered_params(
    service: ProtectionService, borrower: str
) -> dict[str, Any] | None:
    """The borrower's signed risk mandate — **without the signature**.

    The signature is deliberately omitted. It is the borrower's authorisation, it is of no use in
    explaining anything, and a tool result is text the model may echo. Withholding it means it
    cannot be echoed.
    """
    registered = service.params_of(borrower)
    if registered is None:
        return None
    params, _signature = registered
    return {
        "borrower": params.borrower,
        "hf_trigger_bps": params.hf_trigger_bps,
        "hf_target_base_bps": params.hf_target_base_bps,
        "hf_target_max_bps": params.hf_target_max_bps,
        "vol_coeff_k": params.vol_coeff_k,
        "max_slippage_bps": params.max_slippage_bps,
        "max_cost_bps": params.max_cost_bps,
        "allowed_collaterals": list(params.allowed_collaterals),
        "allowed_collateral_symbols": [
            TOKEN_SYMBOLS.get(a, a) for a in params.allowed_collaterals
        ],
        "nonce": params.nonce,
        "deadline": params.deadline,
    }


async def t_assess(service: ProtectionService, borrower: str) -> dict[str, Any]:
    """Run the decision pipeline for a registered borrower. Read-only: nothing is submitted.

    These are the authoritative numbers — the same ones ``GET /positions/{b}/assessment``
    returns, produced by the same code.
    """
    registered = service.params_of(borrower)
    if registered is None:
        return {"error": "borrower not registered; no signed mandate to assess against"}
    params, _ = registered
    sigma = sigma_for(service, params)
    result = await service.assess(params, sigma=sigma)
    return {
        "borrower": borrower,
        "hf": result.hf,
        "hf_target": result.hf_target,
        "repay_amount": result.repay_amount,
        "collateral_asset": result.collateral_asset,
        "collateral_symbol": TOKEN_SYMBOLS.get(result.collateral_asset, result.collateral_asset),
        "est_cost_bps": result.est_cost_bps,
        "viable": result.viable,
        "reason": result.reason,
    }


async def t_risk_signal(service: ProtectionService, borrower: str) -> dict[str, Any]:
    """Realised volatility, breach probability, and the dynamic health-factor target.

    Recovers what the pipeline computes on every assessment and then discards: ``RiskSignal``
    never reaches ``AssessmentResponse``, so σ and the breach probability are invisible over the
    REST API today.
    """
    registered = service.params_of(borrower)
    if registered is None:
        return {"error": "borrower not registered; no signed mandate to derive a target from"}
    params, _ = registered
    snap = await service.snapshot(borrower, params.hf_trigger_bps)
    signal = risk_signal(service, params, snap.hf)
    return {
        "borrower": borrower,
        "hf": snap.hf,
        "sigma": signal.sigma,
        "breach_probability": signal.breach_probability,
        "hf_target_bps": signal.hf_target_bps,
        "hf_target": signal.hf_target_bps / 10_000,
        "hf_trigger_bps": params.hf_trigger_bps,
        "note": "sigma is a rolling standard deviation of oracle prices, not a forecast; the "
                "breach probability is a heuristic early-warning signal.",
    }


async def t_metrics(service: ProtectionService) -> dict[str, Any]:
    """Keeper health: circuit breaker, in-flight locks, decision counters, per-borrower states."""
    m = service.metrics()
    return m.model_dump()


async def t_explain_state(state: str) -> dict[str, Any]:
    """What a ``PositionState`` means and which states it may move to."""
    try:
        parsed = PositionState(state.upper())
    except ValueError:
        return {"error": f"unknown state {state!r}",
                "known_states": [s.value for s in PositionState]}
    return {
        "state": parsed.value,
        "meaning": _STATE_EXPLANATIONS[parsed],
        "may_transition_to": sorted(s.value for s in VALID_TRANSITIONS.get(parsed, frozenset())),
    }


async def t_explain_revert(error_name: str) -> dict[str, Any]:
    """Explain one of the vault's custom errors in the vault's own terms."""
    name = error_name.strip().removesuffix("()")
    explanation = _VAULT_ERRORS.get(name)
    if explanation is None:
        return {"error": f"{name!r} is not a known vault error",
                "known_errors": sorted(_VAULT_ERRORS)}
    return {"error_name": name, "meaning": explanation}


async def t_list_proposals(
    store: AgentStore, *, borrower: str | None = None, status: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Agent proposals and their approval status."""
    parsed: ProposalStatus | None = None
    if status is not None:
        try:
            parsed = ProposalStatus(status.upper())
        except ValueError:
            return [{"error": f"unknown status {status!r}",
                     "known_statuses": [s.value for s in ProposalStatus]}]
    rows = await store.list_proposals(status=parsed, borrower=borrower, limit=limit)
    return [
        {"id": r.id, "borrower": r.borrower, "status": r.status.value, "strategy": r.strategy,
         "created_at": r.created_at, "expires_at": r.expires_at, "rationale": r.rationale,
         "gate_allowed": r.gate.allowed, "gate_blocking": r.gate.blocking,
         "tx_hash": r.tx_hash, "facts": r.facts}
        for r in rows
    ]


async def t_audit_trail(
    store: AgentStore, *, borrower: str | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    """What the agent and its operators have done, newest first."""
    rows = await store.list_audit(borrower=borrower, limit=limit)
    return [
        {"ts": r.ts, "actor": r.actor, "action": r.action.value, "borrower": r.borrower,
         "proposal_id": r.proposal_id, "detail": r.detail}
        for r in rows
    ]


# --- Write-gated tools (crew graph only; never bound to the chat model) -----------------------


async def t_propose_protection(
    store: AgentStore,
    *,
    run_id: str,
    borrower: str,
    facts: dict[str, Any],
    rationale: str,
    gate: PolicyDecision,
    ttl_seconds: int,
    guard_flagged: bool = False,
) -> int:
    """Queue a rescue proposal for human approval. Submits nothing.

    Refuses outright when the gate did not allow the proposal. Belt and braces — the graph only
    routes here on an allowed gate — but the assertion means a future refactor cannot quietly
    turn a blocked proposal into a pending one that somebody then approves.
    """
    if not gate.allowed:
        raise ValueError(
            f"refusing to queue a proposal the policy gate blocked: {gate.blocking}"
        )
    return await store.insert_proposal(
        run_id=run_id, borrower=borrower, strategy="protect_now", facts=facts, gate=gate,
        rationale=rationale, ttl_seconds=ttl_seconds, guard_flagged=guard_flagged,
    )


async def t_propose_tuning(
    store: AgentStore,
    *,
    run_id: str,
    borrower: str,
    params: RiskParams,
    field_name: TunableField,
    suggested_value: int,
    rationale: str,
) -> int:
    """Record a **re-sign request** for one risk-mandate field (FR-21).

    This cannot and must not change anything by itself. ``RiskParams`` is signed by the borrower
    and re-verified on chain by the vault; a field altered after signing makes the signature
    invalid and ``executeProtection`` reverts ``BadSignature``. So the only honest output is the
    complete new mandate for the borrower to sign, which is what is stored here.
    """
    current_value = int(getattr(params, field_name))
    proposed = params.model_copy(update={field_name: suggested_value})
    # Re-validate: the mandate must still satisfy RiskParams' own invariants (notably the ordered
    # HF band), or we would be asking the borrower to sign something the API would reject.
    validated = RiskParams.model_validate(proposed.model_dump())
    return await store.insert_tuning(
        run_id=run_id, borrower=borrower, field_name=field_name, current_value=current_value,
        suggested_value=suggested_value, rationale=rationale,
        eip712_payload=validated.eip712_message(),
    )


# --- Internals -------------------------------------------------------------------------------


def sigma_for(service: ProtectionService, params: RiskParams) -> float:
    """σ from the monitor the worker loop actually fills, mirroring ``protect()``."""
    if not params.allowed_collaterals:
        return 0.0
    return service.monitor.sigma_for(params.allowed_collaterals[0])


def risk_signal(service: ProtectionService, params: RiskParams, hf: float) -> RiskSignal:
    return assess_risk(
        hf,
        sigma_for(service, params),
        base_bps=params.hf_target_base_bps,
        max_bps=params.hf_target_max_bps,
        k=params.vol_coeff_k,
    )


def debt_asset_facts() -> tuple[str, int]:
    """The debt asset the pipeline uses, and its decimals.

    ``AssessmentPipeline._infer_debt_asset`` is hardcoded to USDC and documented as the hook for
    multi-debt support; this mirrors that single point rather than re-deriving it, so the two move
    together when it generalises.
    """
    usdc = TOKENS["USDC"]
    return usdc, TOKEN_DECIMALS[usdc]
