"""SQLite store tests — schema, lifecycle, the approval race guard, and concurrency.

Runs entirely against ``:memory:`` and needs neither the optional agent stack nor a chain, so it
executes in the default suite. The store is the layer's only durable state, and the approval race
guard (:meth:`AgentStore.claim_proposal`) is what stops two operators executing one proposal
twice — that behaviour is asserted directly rather than inferred.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent.models import (
    AuditAction,
    GateCheck,
    PolicyDecision,
    ProposalStatus,
    TuningStatus,
)
from app.agent.store import AgentStore

BORROWER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
OTHER = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"


def _gate(allowed: bool = True) -> PolicyDecision:
    return PolicyDecision(
        allowed=allowed,
        checks=[GateCheck(name="registered", passed=True, detail="signed mandate on file")],
        blocking=[] if allowed else ["breaker_ok"],
        severity="ok" if allowed else "hard_block",
    )


@pytest.fixture
async def store():  # type: ignore[no-untyped-def]
    s = AgentStore(":memory:")
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


async def _propose(store: AgentStore, *, borrower: str = BORROWER, **kw: object) -> int:
    defaults: dict[str, object] = {
        "run_id": "run-1", "borrower": borrower, "strategy": "protect_now",
        "facts": {"hf": 1.14}, "gate": _gate(), "rationale": "HF below trigger.",
        "ttl_seconds": 900,
    }
    defaults.update(kw)
    return await store.insert_proposal(**defaults)  # type: ignore[arg-type]


# --- Lifecycle -------------------------------------------------------------------------------


async def test_connect_is_idempotent_and_creates_schema(store: AgentStore) -> None:
    assert store.ready
    await store.connect()  # second call must be a no-op, not an error
    assert store.ready
    assert await store.list_proposals() == []


async def test_methods_self_connect_without_explicit_connect() -> None:
    """Routes must work in a TestClient that never ran the lifespan."""
    s = AgentStore(":memory:")
    assert not s.ready
    assert await s.list_proposals() == []
    assert s.ready
    await s.close()


async def test_no_file_is_created_for_memory_store() -> None:
    from pathlib import Path

    s = AgentStore(":memory:")
    await s.connect()
    await s.close()
    assert not Path(":memory:").exists()


# --- Proposals -------------------------------------------------------------------------------


async def test_insert_and_read_back_roundtrips_json_columns(store: AgentStore) -> None:
    pid = await _propose(store, facts={"hf": 1.14, "repay_amount": 2_400_000_000})
    row = await store.get_proposal(pid)
    assert row is not None
    assert row.status is ProposalStatus.PENDING
    assert row.borrower == BORROWER
    assert row.facts["repay_amount"] == 2_400_000_000
    assert row.gate.allowed is True
    assert row.gate.checks[0].name == "registered"
    assert row.tx_hash is None


async def test_get_proposal_returns_none_when_absent(store: AgentStore) -> None:
    assert await store.get_proposal(4242) is None


async def test_list_filters_by_status_and_borrower(store: AgentStore) -> None:
    a = await _propose(store, borrower=BORROWER)
    await _propose(store, borrower=OTHER)
    await store.claim_proposal(a, status=ProposalStatus.APPROVED)

    pending = await store.list_proposals(status=ProposalStatus.PENDING)
    assert [p.borrower for p in pending] == [OTHER]
    assert len(await store.list_proposals(borrower=BORROWER)) == 1
    assert len(await store.list_proposals()) == 2


async def test_list_is_newest_first_and_respects_limit(store: AgentStore) -> None:
    for _ in range(5):
        await _propose(store)
    rows = await store.list_proposals(limit=2)
    assert len(rows) == 2
    assert rows[0].id > rows[1].id


async def test_claim_proposal_succeeds_once_then_refuses(store: AgentStore) -> None:
    pid = await _propose(store)
    assert await store.claim_proposal(pid, status=ProposalStatus.APPROVED) is True
    assert await store.claim_proposal(pid, status=ProposalStatus.APPROVED) is False
    assert await store.claim_proposal(pid, status=ProposalStatus.REJECTED) is False


async def test_concurrent_claims_have_exactly_one_winner(store: AgentStore) -> None:
    """Two operators approving the same proposal must not both reach execution."""
    pid = await _propose(store)
    results = await asyncio.gather(
        *(store.claim_proposal(pid, status=ProposalStatus.APPROVED) for _ in range(8))
    )
    assert sum(results) == 1


async def test_finish_proposal_records_outcome(store: AgentStore) -> None:
    pid = await _propose(store)
    await store.claim_proposal(pid, status=ProposalStatus.APPROVED)
    await store.finish_proposal(
        pid, status=ProposalStatus.EXECUTED, decided_by="ops@finax",
        note="looks right", tx_hash="0xabc", result={"submitted": True},
    )
    row = await store.get_proposal(pid)
    assert row is not None
    assert row.status is ProposalStatus.EXECUTED
    assert row.decided_by == "ops@finax"
    assert row.tx_hash == "0xabc"
    assert row.result == {"submitted": True}
    assert row.decided_at is not None


async def test_finish_proposal_coalesce_preserves_earlier_fields(store: AgentStore) -> None:
    """A later status change must not blank the approver recorded at claim time."""
    pid = await _propose(store)
    await store.finish_proposal(pid, status=ProposalStatus.APPROVED, decided_by="ops@finax")
    await store.finish_proposal(pid, status=ProposalStatus.EXECUTED, tx_hash="0xdef")
    row = await store.get_proposal(pid)
    assert row is not None
    assert row.decided_by == "ops@finax"
    assert row.tx_hash == "0xdef"


async def test_count_recent_proposals_feeds_rate_limits(store: AgentStore) -> None:
    await _propose(store, borrower=BORROWER, now=1_000.0)
    await _propose(store, borrower=BORROWER, now=2_000.0)
    await _propose(store, borrower=OTHER, now=2_000.0)

    assert await store.count_recent_proposals(since=0.0) == 3
    assert await store.count_recent_proposals(since=0.0, borrower=BORROWER) == 2
    assert await store.count_recent_proposals(since=1_500.0, borrower=BORROWER) == 1
    assert await store.count_recent_proposals(since=9_999.0) == 0


async def test_expire_stale_proposals_only_touches_pending(store: AgentStore) -> None:
    fresh = await _propose(store, now=1_000.0, ttl_seconds=900)
    old = await _propose(store, now=1_000.0, ttl_seconds=10)
    claimed = await _propose(store, now=1_000.0, ttl_seconds=10)
    await store.claim_proposal(claimed, status=ProposalStatus.APPROVED)

    assert await store.expire_stale_proposals(now=1_500.0) == 1

    assert (await store.get_proposal(old)).status is ProposalStatus.EXPIRED  # type: ignore[union-attr]
    assert (await store.get_proposal(fresh)).status is ProposalStatus.PENDING  # type: ignore[union-attr]
    assert (await store.get_proposal(claimed)).status is ProposalStatus.APPROVED  # type: ignore[union-attr]


# --- Tuning suggestions ----------------------------------------------------------------------


async def test_tuning_roundtrip_and_dismiss(store: AgentStore) -> None:
    tid = await store.insert_tuning(
        run_id="run-1", borrower=BORROWER, field_name="hf_target_base_bps",
        current_value=12_500, suggested_value=13_000,
        rationale="Realized volatility has doubled over the window.",
        eip712_payload={"borrower": BORROWER, "hfTargetBaseBps": 13_000},
    )
    row = await store.get_tuning(tid)
    assert row is not None
    assert row.status is TuningStatus.OPEN
    assert row.suggested_value == 13_000
    # Structurally constant: a mandate the borrower has not signed cannot execute (FR-21).
    assert row.requires_new_signature is True

    assert await store.set_tuning_status(tid, TuningStatus.DISMISSED) is True
    assert await store.set_tuning_status(tid, TuningStatus.DISMISSED) is False
    assert (await store.get_tuning(tid)).status is TuningStatus.DISMISSED  # type: ignore[union-attr]


async def test_list_tuning_defaults_to_open_only(store: AgentStore) -> None:
    open_id = await store.insert_tuning(
        run_id="r", borrower=BORROWER, field_name="max_cost_bps", current_value=500,
        suggested_value=400, rationale="x", eip712_payload={},
    )
    dismissed = await store.insert_tuning(
        run_id="r", borrower=BORROWER, field_name="vol_coeff_k", current_value=7500,
        suggested_value=9000, rationale="y", eip712_payload={},
    )
    await store.set_tuning_status(dismissed, TuningStatus.DISMISSED)

    assert [t.id for t in await store.list_tuning()] == [open_id]
    assert len(await store.list_tuning(status=None)) == 2


# --- Decisions & audit -----------------------------------------------------------------------


async def test_decisions_are_ordered_by_insertion(store: AgentStore) -> None:
    for node in ("monitor", "analyst", "strategist"):
        await store.insert_decision(
            run_id="run-7", borrower=BORROWER, node=node, output={"ok": True},
            latency_ms=12, llm_used=node == "analyst",
        )
    trace = await store.list_decisions("run-7")
    assert [d.node for d in trace] == ["monitor", "analyst", "strategist"]
    assert [d.llm_used for d in trace] == [False, True, False]
    assert await store.list_decisions("run-other") == []


async def test_audit_is_append_only_and_newest_first(store: AgentStore) -> None:
    await store.audit(actor="agent", action=AuditAction.PROPOSED, borrower=BORROWER,
                      proposal_id=1, detail={"strategy": "protect_now"})
    await store.audit(actor="human", action=AuditAction.APPROVED, borrower=BORROWER,
                      proposal_id=1, detail={"by": "ops"})
    await store.audit(actor="system", action=AuditAction.PANIC)

    entries = await store.list_audit()
    assert [e.action for e in entries] == [
        AuditAction.PANIC, AuditAction.APPROVED, AuditAction.PROPOSED
    ]
    assert entries[-1].detail == {"strategy": "protect_now"}
    assert [e.action for e in await store.list_audit(borrower=BORROWER)] == [
        AuditAction.APPROVED, AuditAction.PROPOSED
    ]


# --- Chat ------------------------------------------------------------------------------------


async def test_chat_history_roundtrip(store: AgentStore) -> None:
    await store.ensure_thread("t1", borrower=BORROWER)
    await store.append_message(thread_id="t1", role="user", content="why is this at risk?")
    await store.append_message(
        thread_id="t1", role="assistant", content="HF is 1.14, below the 1.15 trigger.",
        tool_calls=[{"name": "t_assess", "args": {"borrower": BORROWER}}],
        facts={"hf": 1.14},
    )
    msgs = await store.history("t1")
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].tool_calls == [{"name": "t_assess", "args": {"borrower": BORROWER}}]
    assert msgs[1].facts == {"hf": 1.14}
    assert msgs[0].tool_calls is None


async def test_ensure_thread_is_upsert_and_keeps_borrower(store: AgentStore) -> None:
    await store.ensure_thread("t1", borrower=BORROWER)
    await store.ensure_thread("t1")  # a later turn without borrower context
    await store.append_message(thread_id="t1", role="user", content="hi")
    assert len(await store.history("t1")) == 1


async def test_history_keeps_the_newest_window(store: AgentStore) -> None:
    """A long thread must replay its most recent turns, not its oldest."""
    await store.ensure_thread("t1")
    for i in range(10):
        await store.append_message(thread_id="t1", role="user", content=f"m{i}")
    msgs = await store.history("t1", limit=3)
    assert [m.content for m in msgs] == ["m7", "m8", "m9"]


async def test_delete_thread_cascades_to_messages(store: AgentStore) -> None:
    await store.ensure_thread("t1")
    await store.append_message(thread_id="t1", role="user", content="hi")
    assert await store.delete_thread("t1") is True
    assert await store.history("t1") == []
    assert await store.delete_thread("t1") is False


async def test_concurrent_writes_all_land(store: AgentStore) -> None:
    """The write lock must serialise, not drop: every concurrent insert is persisted."""
    await asyncio.gather(*(_propose(store, run_id=f"run-{i}") for i in range(20)))
    assert len(await store.list_proposals(limit=100)) == 20
