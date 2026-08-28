"""API-layer tests — routes over the shared service via FastAPI dependency overrides.

Uses the real ``ProtectionService`` wired with fake pipeline/simulator/submitter so the HTTP
contract (status/assessment/protect/metrics/config) is exercised without a chain. The background
worker is disabled so the app lifespan does not start the autonomous loop.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import observability as obs
from app.config.settings import settings
from app.core.breaker import CircuitBreaker
from app.core.inflight import InFlightRegistry
from app.core.protection_service import ProtectionService
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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "worker_enabled", False)
    svc = ProtectionService(
        FakePipeline(_assessment(), _plan()), FakeMonitor(),  # type: ignore[arg-type]
        inflight=InFlightRegistry(cooldown_seconds=0), breaker=CircuitBreaker(3),
        counters=obs.Counters(), simulator=FakeSimulator(True), submitter=FakeSubmitter(1),
    )
    from app import deps
    from app.main import app

    app.dependency_overrides[deps.get_service] = lambda: svc
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _body() -> dict[str, object]:
    p = _params()
    return {"params": p.model_dump(by_alias=True), "signature": "0x00"}


def test_health_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_post_protect_submits(client: TestClient) -> None:
    r = client.post(f"/positions/{BORROWER}/protect", json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["submitted"] is True
    assert data["state"] == "RESTORED"
    assert data["tx_hash"] == "0xabc"


def test_post_assessment_dry_run(client: TestClient) -> None:
    r = client.post(f"/positions/{BORROWER}/assessment", json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["collateral_asset"] == WETH
    assert data["viable"] is True


def test_path_borrower_mismatch_rejected(client: TestClient) -> None:
    r = client.post("/positions/0x0000000000000000000000000000000000000099/protect", json=_body())
    assert r.status_code == 400


def test_metrics_after_protect(client: TestClient) -> None:
    client.post(f"/positions/{BORROWER}/protect", json=_body())
    r = client.get("/metrics")
    assert r.status_code == 200
    m = r.json()
    assert m["counters"][obs.RESTORED] >= 1
    assert m["registered_positions"] >= 1


def test_breaker_reset_endpoint(client: TestClient) -> None:
    r = client.post("/breaker/reset")
    assert r.status_code == 200
    assert r.json()["breaker_paused"] is False
