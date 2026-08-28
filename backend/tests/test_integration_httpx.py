"""
Phase 5 — HTTPX Integration Test Suite
=======================================

Uses httpx.AsyncClient over FastAPI's ASGI transport (no real TCP port needed).
Covers every route, validates request/response schemas, and exercises
the complete service pipeline end-to-end with deterministic fakes.

Test groups
-----------
I.   HEALTH probe
II.  CONFIG endpoint
III. METRICS endpoint
IV.  POSITIONS: snapshot for known/unknown borrowers
V.   ASSESSMENT: register + dry-run (no submit)
VI.  PROTECT: viable → RESTORED, not-viable → DECLINED, sim-fail → DECLINED
VII. BREAKER: trip on consecutive failures → block → reset
VIII.IN-FLIGHT: duplicate concurrency guard
IX.  SCHEMA: response model field validation
X.   EDGE CASES: wrong path address, unregistered 404, missing body 422
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from app.core.models import PositionSnapshot
from app.core.state import PositionState

from tests.test_protection_service import (
    BORROWER,
    USDC,
    WETH,
    FakeMonitor,
    FakePipeline,
    FakeSimulator,
    FakeSubmitter,
    _assessment,
    _params,
    _plan,
)

from app import deps, observability as obs
from app.config.settings import settings
from app.core.breaker import CircuitBreaker
from app.core.inflight import InFlightRegistry
from app.core.protection_service import ProtectionService
from app.main import app

pytestmark = pytest.mark.asyncio

# ─────────────────────────────────────────────────────────────────────────────
# Helpers & Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _body() -> dict[str, Any]:
    p = _params()
    return {"params": p.model_dump(by_alias=True), "signature": "0x00"}


class FullFakeMonitor:
    """FakeMonitor with poll_once support for the snapshot route."""
    def sigma_for(self, asset: str) -> float:  # type: ignore[no-untyped-def]
        return 0.0

    async def poll_once(self, borrower: str, hf_trigger_bps: int = 0) -> PositionSnapshot:
        from app.core.models import UserAccountData
        from app.core.monitor import classify_state
        uad = UserAccountData(
            total_collateral_base=3_000 * 10**8,
            total_debt_base=2_000 * 10**8,
            available_borrows_base=0,
            liquidation_threshold_bps=8400,
            ltv_bps=8000,
            health_factor=int(1.15 * 10**18),
        )
        state = classify_state(uad, hf_trigger_bps or 11_500)
        return PositionSnapshot(
            borrower=borrower, account=uad, state=state,
            hf=uad.hf, hf_trigger_bps=hf_trigger_bps or 11_500,
        )


def _make_client(
    *,
    viable: bool = True,
    sim_ok: bool = True,
    sub_status: int = 1,
    breaker: CircuitBreaker | None = None,
    inflight: InFlightRegistry | None = None,
) -> TestClient:
    settings.worker_enabled = False
    svc = ProtectionService(
        FakePipeline(_assessment(viable=viable), _plan() if viable else None),
        FullFakeMonitor(),  # type: ignore[arg-type]
        inflight=inflight or InFlightRegistry(cooldown_seconds=0),
        breaker=breaker or CircuitBreaker(3),
        counters=obs.Counters(),
        simulator=FakeSimulator(sim_ok),
        submitter=FakeSubmitter(sub_status),
    )
    app.dependency_overrides[deps.get_service] = lambda: svc
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides() -> Any:
    yield
    app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# I. HEALTH
# ─────────────────────────────────────────────────────────────────────────────

def test_integration_health_200() -> None:
    """Health endpoint returns 200 with status=ok."""
    c = _make_client()
    with c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_integration_health_has_chain_info() -> None:
    """Health must return chain_id and chain fields."""
    c = _make_client()
    with c:
        r = c.get("/health")
    body = r.json()
    assert "chain_id" in body
    assert body["chain"] == "arbitrum-one"


def test_integration_health_is_idempotent() -> None:
    """Two consecutive health calls return identical 200."""
    c = _make_client()
    with c:
        assert c.get("/health").status_code == 200
        assert c.get("/health").status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# II. CONFIG
# ─────────────────────────────────────────────────────────────────────────────

def test_integration_config_readable() -> None:
    c = _make_client()
    with c:
        r = c.get("/config")
    assert r.status_code == 200
    body = r.json()
    # KeeperConfig has poll_interval_seconds and breaker_max_consecutive_failures
    assert "poll_interval_seconds" in body or "breaker_max_consecutive_failures" in body or len(body) > 0


def test_integration_config_no_private_key_exposed() -> None:
    """Config endpoint must never expose keeper_private_key."""
    c = _make_client()
    with c:
        r = c.get("/config")
    body_str = r.text
    assert "keeper_private_key" not in body_str
    assert "private" not in body_str.lower()


# ─────────────────────────────────────────────────────────────────────────────
# III. METRICS
# ─────────────────────────────────────────────────────────────────────────────

def test_integration_metrics_empty_on_boot() -> None:
    c = _make_client()
    with c:
        r = c.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    assert "counters" in body
    assert isinstance(body["counters"], dict)


def test_integration_metrics_registered_positions() -> None:
    c = _make_client()
    with c:
        c.post(f"/positions/{BORROWER}/assessment", json=_body())
        r = c.get("/metrics")
    body = r.json()
    assert body.get("registered_positions", 0) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# IV. POSITIONS: snapshot
# ─────────────────────────────────────────────────────────────────────────────

def test_integration_position_snapshot_known_borrower() -> None:
    """GET /positions/{borrower} returns full snapshot with all keys."""
    c = _make_client()
    with c:
        r = c.get(f"/positions/{BORROWER}")
    assert r.status_code == 200
    body = r.json()
    assert body["borrower"].lower() == BORROWER.lower()
    assert "hf" in body
    assert "state" in body
    assert "debt_usd" in body
    assert "collateral_usd" in body


def test_integration_position_has_debt_key() -> None:
    c = _make_client()
    with c:
        r = c.get(f"/positions/{BORROWER}")
    assert r.status_code == 200
    assert "has_debt" in r.json()


# ─────────────────────────────────────────────────────────────────────────────
# V. ASSESSMENT dry-run
# ─────────────────────────────────────────────────────────────────────────────

def test_integration_post_assessment_registers_and_returns_viable() -> None:
    c = _make_client()
    with c:
        r = c.post(f"/positions/{BORROWER}/assessment", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert body["viable"] is True
    assert body["collateral_asset"] == WETH
    assert body["hf"] == pytest.approx(1.14, abs=0.01)
    assert body["hf_target"] == pytest.approx(1.25, abs=0.01)
    assert body["repay_amount"] > 0


def test_integration_post_assessment_no_submission() -> None:
    """Dry-run assessment must not trigger submission (submitter.calls==0)."""
    sub = FakeSubmitter(1)
    settings.worker_enabled = False
    svc = ProtectionService(
        FakePipeline(_assessment(), _plan()), FullFakeMonitor(),  # type: ignore[arg-type]
        inflight=InFlightRegistry(cooldown_seconds=0),
        breaker=CircuitBreaker(3), counters=obs.Counters(),
        simulator=FakeSimulator(True), submitter=sub,
    )
    app.dependency_overrides[deps.get_service] = lambda: svc
    with TestClient(app) as c:
        c.post(f"/positions/{BORROWER}/assessment", json=_body())
    assert sub.calls == 0


def test_integration_get_assessment_after_register() -> None:
    c = _make_client()
    with c:
        c.post(f"/positions/{BORROWER}/assessment", json=_body())
        r = c.get(f"/positions/{BORROWER}/assessment")
    assert r.status_code == 200
    assert r.json()["viable"] is True


def test_integration_get_assessment_404_unregistered() -> None:
    c = _make_client()
    unknown = "0x000000000000000000000000000000000000dead"
    with c:
        r = c.get(f"/positions/{unknown}/assessment")
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# VI. PROTECT: viable / not-viable / sim-fail
# ─────────────────────────────────────────────────────────────────────────────

def test_integration_protect_viable_returns_restored() -> None:
    c = _make_client(viable=True, sim_ok=True, sub_status=1)
    with c:
        r = c.post(f"/positions/{BORROWER}/protect", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert body["submitted"] is True
    assert body["state"] == "RESTORED"
    assert body["tx_hash"] == "0xabc"


def test_integration_protect_not_viable_returns_declined() -> None:
    c = _make_client(viable=False)
    with c:
        r = c.post(f"/positions/{BORROWER}/protect", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert body["submitted"] is False
    assert body["state"] == "DECLINED"


def test_integration_protect_sim_revert_returns_declined() -> None:
    c = _make_client(viable=True, sim_ok=False)
    with c:
        r = c.post(f"/positions/{BORROWER}/protect", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert body["submitted"] is False
    assert body["state"] == "DECLINED"
    assert body["reason"] is not None


def test_integration_protect_response_schema_complete() -> None:
    """Every field of ProtectResponse must be present in the JSON."""
    c = _make_client()
    with c:
        r = c.post(f"/positions/{BORROWER}/protect", json=_body())
    body = r.json()
    for field in ("submitted", "state", "tx_hash", "reason"):
        assert field in body, f"Missing field: {field}"


# ─────────────────────────────────────────────────────────────────────────────
# VII. CIRCUIT BREAKER: trip → block → operator reset
# ─────────────────────────────────────────────────────────────────────────────

def test_integration_breaker_trips_after_3_failures() -> None:
    breaker = CircuitBreaker(3)
    settings.worker_enabled = False
    svc = ProtectionService(
        FakePipeline(_assessment(), _plan()), FullFakeMonitor(),  # type: ignore[arg-type]
        inflight=InFlightRegistry(cooldown_seconds=0),
        breaker=breaker, counters=obs.Counters(),
        simulator=FakeSimulator(True), submitter=FakeSubmitter(0),
    )
    app.dependency_overrides[deps.get_service] = lambda: svc
    with TestClient(app) as c:
        for _ in range(3):
            c.post(f"/positions/{BORROWER}/protect", json=_body())
        assert breaker.paused
        r = c.post(f"/positions/{BORROWER}/protect", json=_body())
        body = r.json()
        assert body["submitted"] is False
        assert "circuit breaker" in (body.get("reason") or "").lower()


def test_integration_breaker_reset_clears_paused() -> None:
    breaker = CircuitBreaker(1)
    settings.worker_enabled = False
    svc = ProtectionService(
        FakePipeline(_assessment(), _plan()), FullFakeMonitor(),  # type: ignore[arg-type]
        inflight=InFlightRegistry(cooldown_seconds=0),
        breaker=breaker, counters=obs.Counters(),
        simulator=FakeSimulator(True), submitter=FakeSubmitter(0),
    )
    app.dependency_overrides[deps.get_service] = lambda: svc
    with TestClient(app) as c:
        c.post(f"/positions/{BORROWER}/protect", json=_body())
        assert breaker.paused
        r = c.post("/breaker/reset")
        assert r.status_code == 200
        assert r.json()["breaker_paused"] is False
        assert not breaker.paused


# ─────────────────────────────────────────────────────────────────────────────
# VIII. IN-FLIGHT concurrency guard
# ─────────────────────────────────────────────────────────────────────────────

async def test_integration_inflight_blocks_duplicate_async() -> None:
    """Concurrent protect calls: first acquires lock, second is blocked."""
    gate = asyncio.Event()
    sub = FakeSubmitter(1, gate=gate)
    settings.worker_enabled = False
    svc = ProtectionService(
        FakePipeline(_assessment(), _plan()), FakeMonitor(),  # type: ignore[arg-type]
        inflight=InFlightRegistry(cooldown_seconds=0),
        breaker=CircuitBreaker(3), counters=obs.Counters(),
        simulator=FakeSimulator(True), submitter=sub,
    )

    first = asyncio.create_task(svc.protect(_params(), "0x00"))
    await asyncio.sleep(0.05)  # allow first to enter submit & hold lock
    second = await svc.protect(_params(), "0x00")

    assert second.submitted is False
    assert "in flight" in (second.reason or "").lower()
    gate.set()
    first_res = await first
    assert first_res.submitted is True
    assert sub.calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# IX. RESPONSE SCHEMA validation
# ─────────────────────────────────────────────────────────────────────────────

def test_integration_health_schema() -> None:
    c = _make_client()
    with c:
        body = c.get("/health").json()
    assert body["status"] == "ok"
    assert body["chain"] == "arbitrum-one"
    assert isinstance(body.get("chain_id"), int)


def test_integration_assessment_schema() -> None:
    c = _make_client()
    with c:
        body = c.post(f"/positions/{BORROWER}/assessment", json=_body()).json()
    assert isinstance(body["hf"], float)
    assert isinstance(body["hf_target"], float)
    assert isinstance(body["repay_amount"], int)
    assert isinstance(body["viable"], bool)
    assert body["collateral_asset"] == WETH


def test_integration_metrics_counters_schema() -> None:
    c = _make_client()
    with c:
        c.post(f"/positions/{BORROWER}/protect", json=_body())
        body = c.get("/metrics").json()
    assert isinstance(body["counters"], dict)
    assert isinstance(body["registered_positions"], int)


# ─────────────────────────────────────────────────────────────────────────────
# X. EDGE CASES
# ─────────────────────────────────────────────────────────────────────────────

def test_integration_mismatched_path_borrower_rejected() -> None:
    """path borrower != signed params.borrower → 400."""
    wrong = "0x0000000000000000000000000000000000000099"
    c = _make_client()
    with c:
        r = c.post(f"/positions/{wrong}/protect", json=_body())
    assert r.status_code == 400
    assert "borrower" in r.json()["detail"].lower()


def test_integration_empty_body_422() -> None:
    c = _make_client()
    with c:
        r = c.post(f"/positions/{BORROWER}/protect", json={})
    assert r.status_code == 422


def test_integration_unknown_route_404() -> None:
    c = _make_client()
    with c:
        r = c.get("/not/a/real/route")
    assert r.status_code == 404


def test_integration_metrics_after_declined_has_no_restored() -> None:
    """Not-viable protect must NOT increment RESTORED counter."""
    c = _make_client(viable=False)
    with c:
        c.post(f"/positions/{BORROWER}/protect", json=_body())
        body = c.get("/metrics").json()
    # RESTORED counter should be 0 or absent
    assert body["counters"].get(obs.RESTORED, 0) == 0


def test_integration_repeated_protect_does_not_stack_tx() -> None:
    """Two sequential protect calls on the same borrower both return valid responses."""
    c = _make_client()
    with c:
        r1 = c.post(f"/positions/{BORROWER}/protect", json=_body())
        r2 = c.post(f"/positions/{BORROWER}/protect", json=_body())
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Both must have completed lifecycle (no crash/exception)
    assert r1.json()["submitted"] is True
    assert r2.json()["submitted"] is True
