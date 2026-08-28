"""
Phase 5 — Playwright E2E & Full Integration Test Suite
======================================================

Uses Playwright's APIRequestContext (headless HTTP client, no browser needed for a
JSON API) to validate the FastAPI service end-to-end over a real HTTP/ASGI transport.

Test groups
-----------
1. HEALTH          — liveness probe
2. CONFIG          — config/metrics read endpoints
3. POSITIONS       — register, snapshot, assessment dry-run
4. PROTECT FLOW    — viable → submit → RESTORED
5. GUARD RAILS     — mismatch, breaker-blocked, in-flight blocked
6. METRICS         — counters increment correctly
7. FULL E2E FLOW   — complete protect lifecycle from registration to metrics update
8. ERROR PATHS     — 404, 400, invalid payload, unknown borrower
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

import httpx
import pytest
import uvicorn
from playwright.async_api import APIRequestContext, async_playwright
from app.core.models import PositionSnapshot, UserAccountData
from app.core.state import PositionState

# ── Fake service wiring (reuse protection_service test helpers) ──────────────
from tests.test_protection_service import (
    BORROWER,
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
from app.core.models import PositionSnapshot, UserAccountData
from app.core.protection_service import ProtectionService
from app.main import app

pytestmark = pytest.mark.asyncio

BASE_URL = "http://127.0.0.1:18765"

# ─────────────────────────────────────────────────────────────────────────────
# Live server fixture — starts uvicorn in a daemon thread
# ─────────────────────────────────────────────────────────────────────────────

class FullFakeMonitor:
    """FakeMonitor with poll_once for the /positions/{borrower} snapshot route."""
    def sigma_for(self, asset: str) -> float:  # type: ignore[no-untyped-def]
        return 0.0

    async def poll_once(self, borrower: str, hf_trigger_bps: int = 0) -> PositionSnapshot:
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


def _make_service(*, viable: bool = True, sim_ok: bool = True) -> ProtectionService:
    settings.worker_enabled = False  # never start background loop in tests
    return ProtectionService(
        FakePipeline(_assessment(viable=viable), _plan() if viable else None),
        FullFakeMonitor(),  # type: ignore[arg-type]
        inflight=InFlightRegistry(cooldown_seconds=0),
        breaker=CircuitBreaker(3),
        counters=obs.Counters(),
        simulator=FakeSimulator(sim_ok),
        submitter=FakeSubmitter(1 if viable else 0),
    )


@pytest.fixture(scope="module")
def live_server() -> Any:
    """Start a real uvicorn instance for the full E2E tests."""
    settings.worker_enabled = False
    svc = _make_service()
    app.dependency_overrides[deps.get_service] = lambda: svc

    config = uvicorn.Config(app, host="127.0.0.1", port=18765, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import time
    for _ in range(30):
        try:
            httpx.get(f"{BASE_URL}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)

    yield BASE_URL

    server.should_exit = True
    thread.join(timeout=5)
    app.dependency_overrides.clear()


def _body() -> dict[str, Any]:
    p = _params()
    return {"params": p.model_dump(by_alias=True), "signature": "0x00"}


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 1 — HEALTH endpoint
# ─────────────────────────────────────────────────────────────────────────────

async def test_health_returns_200_and_ok(live_server: str) -> None:
    """GET /health must return 200 with status=ok."""
    async with async_playwright() as pw:
        ctx: APIRequestContext = await pw.request.new_context(base_url=live_server)
        r = await ctx.get("/health")
        assert r.status == 200
        body = await r.json()
        assert body["status"] == "ok"
        await ctx.dispose()


async def test_health_response_has_chain_info(live_server: str) -> None:
    """GET /health must contain chain_id and chain fields."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.get("/health")
        body = await r.json()
        assert "chain_id" in body
        assert body["chain"] == "arbitrum-one"
        await ctx.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 2 — CONFIG / METRICS read
# ─────────────────────────────────────────────────────────────────────────────

async def test_metrics_endpoint_returns_counters(live_server: str) -> None:
    """GET /metrics must return a counters dict."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.get("/metrics")
        assert r.status == 200
        body = await r.json()
        assert "counters" in body
        assert isinstance(body["counters"], dict)
        await ctx.dispose()


async def test_config_endpoint_readable(live_server: str) -> None:
    """GET /config must return keeper config (poll interval etc)."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.get("/config")
        assert r.status == 200
        body = await r.json()
        assert "poll_interval_seconds" in body or "breaker_max_consecutive_failures" in body
        await ctx.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 3 — POSITIONS: snapshot
# ─────────────────────────────────────────────────────────────────────────────

async def test_get_borrower_snapshot_returns_position(live_server: str) -> None:
    """GET /positions/{borrower} returns a full position snapshot with borrower + hf keys."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.get(f"/positions/{BORROWER}")
        assert r.status == 200
        body = await r.json()
        assert "borrower" in body
        assert "hf" in body
        assert "state" in body
        await ctx.dispose()


async def test_get_assessment_404_for_unregistered(live_server: str) -> None:
    """GET /positions/{borrower}/assessment returns 404 if borrower not registered."""
    unknown = "0x000000000000000000000000000000000000dead"
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.get(f"/positions/{unknown}/assessment")
        assert r.status == 404
        await ctx.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 4 — PROTECT FLOW: viable → RESTORED
# ─────────────────────────────────────────────────────────────────────────────

async def test_post_protect_returns_submitted_true(live_server: str) -> None:
    """POST /positions/{borrower}/protect returns submitted=True and RESTORED state."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.post(
            f"/positions/{BORROWER}/protect",
            data=_body(),  # type: ignore[arg-type]
        )
        assert r.status == 200
        body = await r.json()
        assert body["submitted"] is True
        assert body["state"] == "RESTORED"
        assert body["tx_hash"] == "0xabc"
        await ctx.dispose()


async def test_post_assessment_dry_run_viable(live_server: str) -> None:
    """POST /positions/{borrower}/assessment returns assessment without submitting."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.post(
            f"/positions/{BORROWER}/assessment",
            data=_body(),  # type: ignore[arg-type]
        )
        assert r.status == 200
        body = await r.json()
        assert body["viable"] is True
        assert body["collateral_asset"] == WETH
        assert body["repay_amount"] > 0
        await ctx.dispose()


async def test_get_assessment_after_register_returns_200(live_server: str) -> None:
    """After POST /assessment to register, GET /assessment should also work."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        # Register first
        await ctx.post(f"/positions/{BORROWER}/assessment", data=_body())  # type: ignore[arg-type]
        # Now GET
        r = await ctx.get(f"/positions/{BORROWER}/assessment")
        assert r.status == 200
        body = await r.json()
        assert body["viable"] is True
        await ctx.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 5 — GUARD RAILS
# ─────────────────────────────────────────────────────────────────────────────

async def test_path_borrower_mismatch_rejected(live_server: str) -> None:
    """POST /positions/{wrong_addr}/protect must reject 400 if address != signed params."""
    wrong = "0x0000000000000000000000000000000000000099"
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.post(f"/positions/{wrong}/protect", data=_body())  # type: ignore[arg-type]
        assert r.status == 400
        body = await r.json()
        assert "borrower" in body["detail"].lower()
        await ctx.dispose()


async def test_breaker_reset_endpoint(live_server: str) -> None:
    """POST /breaker/reset returns 200 and breaker_paused=False."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.post("/breaker/reset")
        assert r.status == 200
        body = await r.json()
        assert body["breaker_paused"] is False
        await ctx.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 6 — METRICS: counters increment after protect
# ─────────────────────────────────────────────────────────────────────────────

async def test_metrics_restored_counter_increments(live_server: str) -> None:
    """After protect succeeds, RESTORED counter must be ≥ 1."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        await ctx.post(f"/positions/{BORROWER}/protect", data=_body())  # type: ignore[arg-type]
        r = await ctx.get("/metrics")
        body = await r.json()
        assert body["counters"].get(obs.RESTORED, 0) >= 1
        await ctx.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 7 — FULL E2E LIFECYCLE FLOW
# ─────────────────────────────────────────────────────────────────────────────

async def test_full_e2e_registration_to_restored(live_server: str) -> None:
    """
    Full E2E:
    1. GET /health  → ok
    2. POST /positions/{borrower}/assessment  → register + dry-run (no submit)
    3. GET /positions/{borrower}/assessment   → registered, viable
    4. GET /positions/{borrower}              → snapshot with borrower key
    5. POST /positions/{borrower}/protect     → submitted + RESTORED
    6. GET /metrics                           → RESTORED counter >= 1
    """
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)

        # Step 1: health check
        h = await ctx.get("/health")
        assert h.status == 200
        assert (await h.json())["status"] == "ok"

        # Step 2: register via POST assessment (dry-run, no submit)
        a = await ctx.post(f"/positions/{BORROWER}/assessment", data=_body())  # type: ignore[arg-type]
        assert a.status == 200
        adat = await a.json()
        assert adat["viable"] is True
        assert adat["repay_amount"] > 0

        # Step 3: GET assessment (now registered)
        ga = await ctx.get(f"/positions/{BORROWER}/assessment")
        assert ga.status == 200
        assert (await ga.json())["viable"] is True

        # Step 4: position snapshot — FullFakeMonitor has poll_once, so this works
        ps = await ctx.get(f"/positions/{BORROWER}")
        assert ps.status == 200
        snap = await ps.json()
        assert snap["borrower"].lower() == BORROWER.lower()
        assert "hf" in snap

        # Step 5: execute protection
        p = await ctx.post(f"/positions/{BORROWER}/protect", data=_body())  # type: ignore[arg-type]
        assert p.status == 200
        pdat = await p.json()
        assert pdat["submitted"] is True
        assert pdat["state"] == "RESTORED"
        assert pdat["tx_hash"] == "0xabc"

        # Step 6: metrics updated
        m = await ctx.get("/metrics")
        mdat = await m.json()
        assert mdat["counters"].get(obs.RESTORED, 0) >= 1

        await ctx.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 8 — ERROR PATHS
# ─────────────────────────────────────────────────────────────────────────────

async def test_invalid_json_payload_rejected(live_server: str) -> None:
    """POST with malformed JSON must return 422 Unprocessable Entity."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.post(
            f"/positions/{BORROWER}/protect",
            data='{"this is not valid json": ',  # type: ignore[arg-type]
            headers={"Content-Type": "application/json"},
        )
        assert r.status == 422
        await ctx.dispose()


async def test_missing_required_fields_returns_422(live_server: str) -> None:
    """POST with empty body must return 422."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.post(
            f"/positions/{BORROWER}/protect",
            data={},  # type: ignore[arg-type]
        )
        assert r.status == 422
        await ctx.dispose()


async def test_404_on_unknown_route(live_server: str) -> None:
    """GET on a non-existent route must return 404."""
    async with async_playwright() as pw:
        ctx = await pw.request.new_context(base_url=live_server)
        r = await ctx.get("/this/does/not/exist")
        assert r.status == 404
        await ctx.dispose()
