"""Unit tests for the ProtectionService orchestration (FR-8, FR-16, FR-17) with fakes.

No chain: fake pipeline/simulator/submitter let us drive every branch deterministically —
viable→submit→RESTORED, sim-revert→DECLINED, revert→breaker trip, and the in-flight lock
blocking a duplicate rescue.
"""
from __future__ import annotations

import asyncio

import pytest

from app import observability as obs
from app.core.breaker import CircuitBreaker
from app.core.inflight import InFlightRegistry
from app.core.models import (
    AssessmentResponse,
    RescuePlan,
    RiskParams,
    SimulationResult,
    SubmissionResult,
)
from app.core.protection_service import ProtectionService
from app.core.state import PositionState

pytestmark = pytest.mark.asyncio

WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
BORROWER = "0x0000000000000000000000000000000000000011"


def _params() -> RiskParams:
    return RiskParams(
        borrower=BORROWER, hf_trigger_bps=11_500, hf_target_base_bps=12_500, vol_coeff_k=7_500,
        hf_target_max_bps=14_000, max_slippage_bps=100, max_cost_bps=500,
        allowed_collaterals=[WETH], nonce=1, deadline=2_000_000_000,
    )


def _plan() -> RescuePlan:
    return RescuePlan(
        borrower=BORROWER, debt_asset=USDC, repay_amount=1_000_000_000, collateral_asset=WETH,
        fee_tier=500, amount_in=350_000_000_000_000_000, hf_target_bps=12_500, max_slippage_bps=100,
    )


def _assessment(viable: bool = True) -> AssessmentResponse:
    return AssessmentResponse(
        hf=1.14, hf_target=1.25, repay_amount=1_000_000_000, collateral_asset=WETH,
        est_cost_bps=15, viable=viable, reason=None if viable else "not viable",
    )


class FakePipeline:
    def __init__(self, response: AssessmentResponse, plan: RescuePlan | None) -> None:
        self._r = response
        self._p = plan

    async def evaluate(self, params, *, sigma=None, gas_cost_base=0):  # type: ignore[no-untyped-def]
        return self._r, self._p

    async def assess(self, params, *, sigma=None):  # type: ignore[no-untyped-def]
        return self._r


class FakeMonitor:
    def sigma_for(self, asset):  # type: ignore[no-untyped-def]
        return 0.0


class FakeSimulator:
    def __init__(self, success: bool = True) -> None:
        self.success = success

    async def simulate(self, plan, params, signature):  # type: ignore[no-untyped-def]
        return SimulationResult(
            success=self.success, repay_amount=plan.repay_amount,
            amount_in_maximum=plan.amount_in, bumps=0,
            revert_reason=None if self.success else "HealthBelowTarget()",
        )


class FakeSubmitter:
    def __init__(self, status: int = 1, gate: asyncio.Event | None = None) -> None:
        self.status = status
        self.calls = 0
        self._gate = gate

    async def submit(self, plan, params, signature, *, repay_amount, amount_in_maximum):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self._gate is not None:
            await self._gate.wait()
        state = PositionState.RESTORED if self.status == 1 else PositionState.REVERTED
        return SubmissionResult(
            tx_hash="0xabc", status=self.status, state=state,
            hf_after=1.30 if self.status == 1 else None, gas_used=100_000,
        )


def _service(pipeline, *, simulator=None, submitter=None, breaker=None, inflight=None):  # type: ignore[no-untyped-def]
    return ProtectionService(
        pipeline, FakeMonitor(),  # type: ignore[arg-type]
        inflight=inflight or InFlightRegistry(cooldown_seconds=0),
        breaker=breaker or CircuitBreaker(3),
        counters=obs.Counters(),
        simulator=simulator, submitter=submitter,
    )


async def test_viable_path_submits_and_restores() -> None:
    svc = _service(FakePipeline(_assessment(), _plan()),
                   simulator=FakeSimulator(True), submitter=FakeSubmitter(1))
    res = await svc.protect(_params(), "0x00")
    assert res.submitted is True
    assert res.state == PositionState.RESTORED
    assert res.tx_hash == "0xabc"
    assert svc.metrics().counters.get(obs.RESTORED) == 1


async def test_not_viable_declines_without_submitting() -> None:
    sub = FakeSubmitter(1)
    svc = _service(FakePipeline(_assessment(viable=False), None),
                   simulator=FakeSimulator(True), submitter=sub)
    res = await svc.protect(_params(), "0x00")
    assert res.submitted is False
    assert res.state == PositionState.DECLINED
    assert sub.calls == 0


async def test_simulation_revert_declines() -> None:
    sub = FakeSubmitter(1)
    svc = _service(FakePipeline(_assessment(), _plan()),
                   simulator=FakeSimulator(False), submitter=sub)
    res = await svc.protect(_params(), "0x00")
    assert res.state == PositionState.DECLINED
    assert "simulation reverted" in (res.reason or "")
    assert sub.calls == 0


async def test_assessment_only_when_no_simulator() -> None:
    svc = _service(FakePipeline(_assessment(), _plan()))
    res = await svc.protect(_params(), "0x00")
    assert res.state == PositionState.READY
    assert res.submitted is False


async def test_three_reverts_trip_breaker_then_block() -> None:
    breaker = CircuitBreaker(3)
    svc = _service(FakePipeline(_assessment(), _plan()),
                   simulator=FakeSimulator(True), submitter=FakeSubmitter(0), breaker=breaker)
    for _ in range(3):
        res = await svc.protect(_params(), "0x00")
        assert res.state == PositionState.REVERTED
    assert breaker.paused
    # Fourth attempt is blocked by the breaker (no submission).
    res = await svc.protect(_params(), "0x00")
    assert res.submitted is False
    assert "circuit breaker paused" in (res.reason or "")
    assert svc.metrics().counters.get(obs.BREAKER_BLOCKED) == 1


async def test_double_submit_blocked_while_in_flight() -> None:
    gate = asyncio.Event()
    sub = FakeSubmitter(1, gate=gate)
    svc = _service(FakePipeline(_assessment(), _plan()),
                   simulator=FakeSimulator(True), submitter=sub)
    # First protect holds the in-flight lock at the gated submit; second must be blocked.
    first = asyncio.create_task(svc.protect(_params(), "0x00"))
    await asyncio.sleep(0.05)  # let `first` reach the awaiting submit (lock held)
    second = await svc.protect(_params(), "0x00")
    assert second.submitted is False
    assert "in flight" in (second.reason or "")
    gate.set()
    first_res = await first
    assert first_res.submitted is True
    assert sub.calls == 1
    assert svc.metrics().counters.get(obs.DOUBLE_SUBMIT_BLOCKED) == 1
