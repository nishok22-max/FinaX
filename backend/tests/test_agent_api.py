"""Agent API tests — the routes, and above all the approval path.

The approval route is the only place in the whole agent layer where a transaction can occur, so
most of this file is about the conditions under which it refuses: a moved position, an expired
proposal, a paused breaker, a withdrawn mandate, a concurrent approver.

Injects the service through ``app.dependency_overrides[deps.get_service]`` — the seam the existing
suite already uses — and the runtime through ``reset_runtime_for_tests``.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.agent._lazy import agent_stack_available
from app.agent.models import GateCheck, PolicyDecision, ProposalStatus
from app.agent.runtime import AgentRuntime, reset_runtime_for_tests
from app.agent.store import AgentStore
from app.config.settings import settings
from app.core.breaker import TripReason
from app.main import app
from tests.test_agent_graph import make_service
from tests.test_protection_service import BORROWER

pytestmark = pytest.mark.skipif(
    not agent_stack_available(), reason='the optional agent extra is not installed ([agent])'
)


@pytest.fixture
def enabled_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the layer as an enabled deployment would.

    ``agent_status()`` reads settings rather than the injected runtime on purpose — it reports
    the real configuration, so an operator is told which precondition is unmet. Tests therefore
    have to configure it, not just inject a runtime.

    ``worker_enabled`` is turned off for the same reason the existing suite does it: the lifespan
    would otherwise start the real polling loop against the configured RPC.
    """
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_model", "fake")
    monkeypatch.setattr(settings, "worker_enabled", False)


@pytest.fixture
async def wired(enabled_settings: None):  # type: ignore[no-untyped-def]
    """A TestClient with a fake service and an in-memory agent store."""
    store = AgentStore(":memory:")
    await store.connect()
    runtime = AgentRuntime(store=store, model="fake", api_key="fake", timeout_s=5.0)
    reset_runtime_for_tests(runtime)

    service = make_service()
    app.dependency_overrides[deps.get_service] = lambda: service
    try:
        with TestClient(app) as client:
            yield client, runtime, service
    finally:
        app.dependency_overrides.clear()
        reset_runtime_for_tests(None)
        await store.close()


def _gate(allowed: bool = True) -> PolicyDecision:
    return PolicyDecision(
        allowed=allowed,
        checks=[GateCheck(name="registered", passed=allowed, detail="signed mandate on file")],
        blocking=[] if allowed else ["breaker_ok"],
        severity="ok" if allowed else "hard_block",
    )


async def _queue(runtime: AgentRuntime, **kw: Any) -> int:
    defaults: dict[str, Any] = {
        "run_id": "r1", "borrower": BORROWER, "strategy": "protect_now",
        "facts": {"hf": 1.14}, "gate": _gate(), "rationale": "HF below trigger.",
        "ttl_seconds": 900,
    }
    defaults.update(kw)
    return await runtime.store.insert_proposal(**defaults)


# --- Status ---------------------------------------------------------------------------------------


async def test_status_reports_an_enabled_layer(wired: Any) -> None:
    client, runtime, _ = wired
    await _queue(runtime)
    body = client.get("/agent/status").json()
    assert body["enabled"] is True
    assert body["model"] == "fake"
    assert body["pending_proposals"] == 1


# --- Listing ---------------------------------------------------------------------------------------


async def test_proposals_list_and_filter(wired: Any) -> None:
    client, runtime, _ = wired
    pid = await _queue(runtime)
    await _queue(runtime, borrower="0x" + "22" * 20)

    assert len(client.get("/agent/proposals").json()) == 2
    only_mine = client.get(f"/agent/proposals?borrower={BORROWER}").json()
    assert [p["id"] for p in only_mine] == [pid]
    assert len(client.get("/agent/proposals?status=PENDING").json()) == 2
    assert client.get("/agent/proposals?status=EXECUTED").json() == []


async def test_unknown_status_filter_is_a_400(wired: Any) -> None:
    client, _, _ = wired
    assert client.get("/agent/proposals?status=MAYBE").status_code == 400


async def test_get_proposal_404s_when_absent(wired: Any) -> None:
    client, _, _ = wired
    assert client.get("/agent/proposals/999").status_code == 404


# --- Reject -----------------------------------------------------------------------------------------


async def test_reject_records_the_decision(wired: Any) -> None:
    client, runtime, _ = wired
    pid = await _queue(runtime)
    body = client.post(f"/agent/proposals/{pid}/reject",
                       json={"rejected_by": "ops", "note": "too costly"}).json()
    assert body["status"] == "REJECTED"
    assert body["decided_by"] == "ops"
    assert "REJECTED" in [a.action.value for a in await runtime.store.list_audit()]


async def test_rejecting_twice_is_a_409(wired: Any) -> None:
    client, runtime, _ = wired
    pid = await _queue(runtime)
    client.post(f"/agent/proposals/{pid}/reject", json={"rejected_by": "ops"})
    assert client.post(f"/agent/proposals/{pid}/reject",
                       json={"rejected_by": "ops"}).status_code == 409


# --- Approve: the happy path -------------------------------------------------------------------------


async def test_approve_executes_through_the_normal_protect_path(wired: Any) -> None:
    """The rescue must run through ProtectionService, exactly as a manual request does."""
    client, runtime, service = wired
    pid = await _queue(runtime)

    response = client.post(f"/agent/proposals/{pid}/approve", json={"approved_by": "ops"})
    assert response.status_code == 200
    body = response.json()

    assert body["proposal"]["status"] == "EXECUTED"
    assert body["protect"]["submitted"] is True
    assert body["protect"]["state"] == "RESTORED"
    assert body["proposal"]["tx_hash"] == body["protect"]["tx_hash"]
    # The keeper's own counters moved — proof the real pipeline ran, not a shortcut.
    assert service.metrics().counters["restored"] == 1


async def test_approve_revalidates_the_gate_before_executing(wired: Any) -> None:
    client, runtime, _ = wired
    pid = await _queue(runtime, gate=_gate())
    body = client.post(f"/agent/proposals/{pid}/approve", json={"approved_by": "ops"}).json()
    # The returned gate is the fresh one, with the full sixteen-check list — not the stored one.
    assert len(body["revalidated_gate"]["checks"]) == 16
    assert body["revalidated_gate"]["allowed"] is True


async def test_approve_writes_the_full_audit_trail(wired: Any) -> None:
    client, runtime, _ = wired
    pid = await _queue(runtime)
    client.post(f"/agent/proposals/{pid}/approve", json={"approved_by": "ops"})
    actions = [a.action.value for a in await runtime.store.list_audit()]
    assert "APPROVED" in actions
    assert "EXECUTED" in actions


# --- Approve: every way it refuses ---------------------------------------------------------------------


async def test_approve_404s_for_an_unknown_proposal(wired: Any) -> None:
    client, _, _ = wired
    assert client.post("/agent/proposals/999/approve",
                       json={"approved_by": "ops"}).status_code == 404


async def test_approve_refuses_a_paused_breaker_and_spends_no_gas(wired: Any) -> None:
    """A hard-blocked gate must stop execution even though the proposal was valid when queued."""
    client, runtime, service = wired
    pid = await _queue(runtime)
    service.breaker.trip(TripReason.MANUAL)

    response = client.post(f"/agent/proposals/{pid}/approve", json={"approved_by": "ops"})
    assert response.status_code == 409
    assert "breaker_ok" in response.json()["detail"]["blocking"]

    row = await runtime.store.get_proposal(pid)
    assert row is not None and row.status is ProposalStatus.STALE
    assert service.metrics().counters.get("submitted", 0) == 0


async def test_approve_refuses_when_the_position_recovered(wired: Any) -> None:
    """The figures in a proposal describe a moment that has passed.

    Swaps the service for one whose HF has climbed back above the trigger between the proposal
    being queued and the operator clicking approve — the case the re-validation exists for.
    """
    client, runtime, _ = wired
    pid = await _queue(runtime)
    app.dependency_overrides[deps.get_service] = lambda: make_service(hf=1.80)

    response = client.post(f"/agent/proposals/{pid}/approve", json={"approved_by": "ops"})
    assert response.status_code == 409
    assert "hf_below_trigger" in response.json()["detail"]["blocking"]

    row = await runtime.store.get_proposal(pid)
    assert row is not None and row.status is ProposalStatus.STALE


async def test_approve_refuses_an_expired_proposal(wired: Any) -> None:
    client, runtime, _ = wired
    pid = await _queue(runtime, ttl_seconds=1, now=time.time() - 100)

    response = client.post(f"/agent/proposals/{pid}/approve", json={"approved_by": "ops"})
    assert response.status_code == 409
    assert "expired" in response.json()["detail"]
    row = await runtime.store.get_proposal(pid)
    assert row is not None and row.status is ProposalStatus.EXPIRED


async def test_an_expired_proposal_can_be_approved_deliberately(wired: Any) -> None:
    """acknowledge_stale overrides the age check only — the gate still had to pass."""
    client, runtime, _ = wired
    pid = await _queue(runtime, ttl_seconds=1, now=time.time() - 100)
    response = client.post(f"/agent/proposals/{pid}/approve",
                           json={"approved_by": "ops", "acknowledge_stale": True})
    assert response.status_code == 200
    assert response.json()["proposal"]["status"] == "EXECUTED"


async def test_approve_refuses_after_the_mandate_is_withdrawn(wired: Any) -> None:
    client, runtime, service = wired
    pid = await _queue(runtime)
    service.unregister(BORROWER)

    response = client.post(f"/agent/proposals/{pid}/approve", json={"approved_by": "ops"})
    assert response.status_code == 409
    assert "no longer has a registered signed mandate" in response.json()["detail"]


async def test_approving_an_already_decided_proposal_is_a_409(wired: Any) -> None:
    client, runtime, _ = wired
    pid = await _queue(runtime)
    assert client.post(f"/agent/proposals/{pid}/approve",
                       json={"approved_by": "ops"}).status_code == 200
    assert client.post(f"/agent/proposals/{pid}/approve",
                       json={"approved_by": "ops2"}).status_code == 409


async def test_approve_requires_an_approver(wired: Any) -> None:
    client, runtime, _ = wired
    pid = await _queue(runtime)
    assert client.post(f"/agent/proposals/{pid}/approve", json={}).status_code == 422


# --- Tuning -------------------------------------------------------------------------------------------


async def test_tuning_list_and_dismiss(wired: Any) -> None:
    client, runtime, _ = wired
    tid = await runtime.store.insert_tuning(
        run_id="r1", borrower=BORROWER, field_name="max_cost_bps", current_value=500,
        suggested_value=300, rationale="Costs have fallen.", eip712_payload={"maxCostBps": 300},
    )
    listed = client.get("/agent/tuning").json()
    assert [t["id"] for t in listed] == [tid]
    assert listed[0]["requires_new_signature"] is True

    assert client.post(f"/agent/tuning/{tid}/dismiss").json()["status"] == "dismissed"
    assert client.get("/agent/tuning").json() == []
    assert client.post(f"/agent/tuning/{tid}/dismiss").status_code == 409


async def test_there_is_no_route_that_applies_a_tuning_suggestion() -> None:
    """FR-21: nothing in the API may mutate a registered, borrower-signed mandate."""
    # app.routes nests included routers in this FastAPI version; the OpenAPI document is the
    # stable enumeration of leaf paths.
    agent_paths = {p for p in app.openapi()["paths"] if p.startswith("/agent")}
    assert agent_paths, "the agent router is not mounted"
    assert not any("apply" in p for p in agent_paths)
    assert "/agent/tuning/{tuning_id}/dismiss" in agent_paths
    # And no agent route writes a mandate at all.
    assert not any(p.endswith("/params") for p in agent_paths)


# --- Audit and the kill switch ----------------------------------------------------------------------------


async def test_audit_endpoint_returns_the_trail(wired: Any) -> None:
    client, runtime, _ = wired
    pid = await _queue(runtime)
    client.post(f"/agent/proposals/{pid}/reject", json={"rejected_by": "ops"})
    trail = client.get("/agent/audit").json()
    assert trail[0]["action"] == "REJECTED"
    assert trail[0]["actor"] == "human"


async def test_panic_trips_the_keepers_own_breaker(wired: Any) -> None:
    """Not just an agent switch: it halts autonomous submission too."""
    client, runtime, service = wired
    body = client.post("/agent/panic", json={"reason": "market dislocation"}).json()

    assert body["paused"] is True
    assert service.breaker.paused is True
    assert service.breaker.allow() is False
    assert "PANIC" in [a.action.value for a in await runtime.store.list_audit()]


async def test_breaker_reset_undoes_panic(wired: Any) -> None:
    client, _, service = wired
    client.post("/agent/panic", json={})
    assert client.post("/breaker/reset").status_code == 200
    assert service.breaker.paused is False


# --- Coexistence with the existing API ------------------------------------------------------------------------


async def test_the_existing_routes_are_unchanged(wired: Any) -> None:
    client, _, _ = wired
    assert client.get("/health").json()["status"] == "ok"
    metrics = client.get("/metrics").json()
    assert "counters" in metrics and isinstance(metrics["counters"], dict)
    assert client.get("/config").status_code == 200


async def test_agent_routes_share_the_services_dependency_override(wired: Any) -> None:
    """The agent sees the same service the REST API does — never a second, drifting instance."""
    client, runtime, service = wired
    service.register(service.params_of(BORROWER)[0], "0x00")  # type: ignore[index]
    pid = await _queue(runtime)
    client.post(f"/agent/proposals/{pid}/approve", json={"approved_by": "ops"})
    # The counter the agent moved is the one /metrics reports.
    assert client.get("/metrics").json()["counters"]["restored"] == 1
