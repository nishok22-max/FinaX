"""Policy-gate tests (FR-19) — every check forced to fail independently, plus its boundaries.

This is the layer's most important test file. The gate is what stands between a model's opinion
and a transaction, and it is a pure function, so there is no excuse for testing it loosely: each
test starts from a proposal that passes cleanly and breaks exactly one thing.

No LLM, no chain, no store — the gate takes backend models and returns a verdict.
"""
from __future__ import annotations

import pytest

from app.agent.models import PolicyLimits
from app.agent.policy import evaluate, repay_value_base
from app.core.models import AssessmentResponse, MetricsSnapshot, PositionSnapshot, RiskParams
from app.core.state import PositionState

BORROWER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WSTETH = "0x5979D7b546E38E414F7E9822514be443A4800529"

USDC_PRICE_BASE = 100_000_000  # $1.00 at 8dp, as Aave's oracle reports it
USDC_DECIMALS = 6
NOW = 1_800_000_000.0


def _params(**kw: object) -> RiskParams:
    base: dict[str, object] = {
        "borrower": BORROWER, "hf_trigger_bps": 11_500, "hf_target_base_bps": 12_500,
        "vol_coeff_k": 7_500, "hf_target_max_bps": 14_000, "max_slippage_bps": 100,
        "max_cost_bps": 500, "allowed_collaterals": [WETH], "nonce": 1,
        "deadline": 2_000_000_000,
    }
    base.update(kw)
    return RiskParams(**base)  # type: ignore[arg-type]


def _snapshot(
    *, hf: float = 1.14, debt_base: int = 2_000 * 10**8, collateral_base: int = 3_000 * 10**8,
    state: PositionState = PositionState.ASSESSING, borrower: str = BORROWER,
) -> PositionSnapshot:
    return PositionSnapshot.model_validate({
        "borrower": borrower,
        "account": {
            "total_collateral_base": collateral_base,
            "total_debt_base": debt_base,
            "available_borrows_base": 0,
            "liquidation_threshold_bps": 8_400,
            "ltv_bps": 8_000,
            "health_factor": int(hf * 10**18),
        },
        "state": state,
        "hf": hf,
        "hf_trigger_bps": 11_500,
    })


def _assessment(**kw: object) -> AssessmentResponse:
    base: dict[str, object] = {
        "hf": 1.14, "hf_target": 1.25, "repay_amount": 400_000_000,  # 400 USDC (6dp)
        "collateral_asset": WETH, "est_cost_bps": 80, "viable": True, "reason": None,
    }
    base.update(kw)
    return AssessmentResponse(**base)  # type: ignore[arg-type]


def _metrics(**kw: object) -> MetricsSnapshot:
    base: dict[str, object] = {
        "breaker_paused": False, "breaker_consecutive_failures": 0,
        "breaker_trip_reason": None, "in_flight_borrowers": [], "registered_positions": 1,
        "counters": {}, "states": {},
    }
    base.update(kw)
    return MetricsSnapshot(**base)  # type: ignore[arg-type]


def _evaluate(**kw: object):  # type: ignore[no-untyped-def]
    """Evaluate a cleanly-passing proposal, overriding only what a test cares about."""
    args: dict[str, object] = {
        "params": _params(), "assessment": _assessment(), "snapshot": _snapshot(),
        "metrics": _metrics(), "limits": PolicyLimits(), "now": NOW,
        "debt_price_base": USDC_PRICE_BASE, "debt_decimals": USDC_DECIMALS,
        "recent_proposals_borrower": 0, "recent_proposals_global": 0,
    }
    args.update(kw)
    return evaluate(**args)  # type: ignore[arg-type]


def _check(decision, name: str):  # type: ignore[no-untyped-def]
    return next(c for c in decision.checks if c.name == name)


# --- The baseline ----------------------------------------------------------------------------


def test_clean_proposal_passes_every_check() -> None:
    d = _evaluate()
    assert d.allowed is True, f"unexpected blockers: {d.blocking}"
    assert d.blocking == []
    assert d.severity == "ok"
    assert all(c.passed for c in d.checks)


def test_all_sixteen_checks_are_always_reported() -> None:
    """The checklist is a fixed shape so the console never renders a ragged table."""
    expected = {
        "registered", "mandate_not_expired", "has_debt", "state_actionable",
        "hf_below_trigger", "hf_gap_material", "assessment_viable", "repay_positive",
        "repay_bounded", "cost_within_mandate", "collateral_allowlisted",
        "target_in_signed_band", "breaker_ok", "not_inflight",
        "rate_limit_borrower", "rate_limit_global",
    }
    assert {c.name for c in _evaluate().checks} == expected
    # Same shape even when the mandate is missing and most checks are unevaluable.
    assert {c.name for c in _evaluate(params=None).checks} == expected


def test_no_check_short_circuits_when_several_fail() -> None:
    d = _evaluate(
        assessment=_assessment(viable=False, repay_amount=0, reason="no eligible collateral"),
        metrics=_metrics(breaker_paused=True, breaker_trip_reason="CONSECUTIVE_FAILURES"),
    )
    assert {"assessment_viable", "repay_positive", "repay_bounded", "breaker_ok"} <= set(d.blocking)
    assert len(d.checks) == 16


# --- Each check, broken on its own -----------------------------------------------------------


def test_unregistered_borrower_blocks_and_explains_dependent_checks() -> None:
    d = _evaluate(params=None)
    assert d.allowed is False
    assert "registered" in d.blocking
    # Mandate-derived checks must fail loudly rather than silently pass.
    for name in ("mandate_not_expired", "cost_within_mandate", "collateral_allowlisted",
                 "target_in_signed_band", "hf_below_trigger"):
        assert _check(d, name).passed is False
        assert "no mandate" in _check(d, name).detail


def test_expired_mandate_blocks() -> None:
    d = _evaluate(params=_params(deadline=int(NOW) - 1))
    assert "mandate_not_expired" in d.blocking


def test_zero_deadline_means_no_expiry() -> None:
    """Matches the vault's own Expired check, where 0 is 'never expires'."""
    assert _check(_evaluate(params=_params(deadline=0)), "mandate_not_expired").passed


def test_no_debt_blocks() -> None:
    d = _evaluate(snapshot=_snapshot(debt_base=0, hf=99.0))
    assert "has_debt" in d.blocking


@pytest.mark.parametrize(
    "state", [PositionState.READY, PositionState.SUBMITTED, PositionState.RESTORED,
              PositionState.DECLINED, PositionState.HEALTHY]
)
def test_non_actionable_states_block(state: PositionState) -> None:
    assert "state_actionable" in _evaluate(snapshot=_snapshot(state=state)).blocking


@pytest.mark.parametrize("state", [PositionState.WATCH, PositionState.ASSESSING])
def test_actionable_states_pass(state: PositionState) -> None:
    assert _check(_evaluate(snapshot=_snapshot(state=state)), "state_actionable").passed


def test_hf_above_trigger_blocks() -> None:
    d = _evaluate(snapshot=_snapshot(hf=1.40), assessment=_assessment(hf=1.40))
    assert "hf_below_trigger" in d.blocking


def test_hf_exactly_at_trigger_passes() -> None:
    """The trigger is inclusive, matching classify_state's `hf_bps <= trigger`."""
    d = _evaluate(snapshot=_snapshot(hf=1.15), assessment=_assessment(hf=1.15, hf_target=1.25))
    assert _check(d, "hf_below_trigger").passed


def test_hf_below_trigger_is_recomputed_not_trusted() -> None:
    """A narrated HF must not be able to move the gate; only the raw WAD integer counts."""
    healthy = _snapshot(hf=1.40)
    d = _evaluate(snapshot=healthy, assessment=_assessment(hf=1.01, hf_target=1.25))
    assert "hf_below_trigger" in d.blocking


def test_immaterial_hf_gap_blocks() -> None:
    d = _evaluate(assessment=_assessment(hf=1.14, hf_target=1.1410))
    assert "hf_gap_material" in d.blocking


def test_hf_gap_exactly_at_minimum_passes() -> None:
    d = _evaluate(
        assessment=_assessment(hf=1.14, hf_target=1.1650),  # 25 bps gap
        limits=PolicyLimits(min_hf_gap_bps=25),
        params=_params(hf_target_base_bps=11_600),
    )
    assert _check(d, "hf_gap_material").passed


def test_non_viable_assessment_blocks_and_surfaces_the_reason() -> None:
    d = _evaluate(assessment=_assessment(viable=False, reason="cost exceeds value protected"))
    assert "assessment_viable" in d.blocking
    assert "cost exceeds value protected" in _check(d, "assessment_viable").detail


def test_zero_repay_blocks() -> None:
    assert "repay_positive" in _evaluate(assessment=_assessment(repay_amount=0)).blocking


def test_oversized_repay_blocks() -> None:
    """51% of a 2,000 USD debt exceeds the 50% ceiling."""
    d = _evaluate(assessment=_assessment(repay_amount=1_020_000_000))  # 1,020 USDC
    assert "repay_bounded" in d.blocking


def test_repay_exactly_at_the_ceiling_passes() -> None:
    d = _evaluate(assessment=_assessment(repay_amount=1_000_000_000))  # 1,000 USDC == 50%
    assert _check(d, "repay_bounded").passed


def test_repay_bound_is_valued_in_usd_not_token_units() -> None:
    """An 18-decimal debt token must not read as an astronomically large repay.

    Comparing raw token units against a 1e8 USD-base debt figure would block every 18-decimal
    debt asset outright; the bound is only meaningful once the amount is priced.
    """
    eighteen_dp_repay = 400 * 10**18  # 400 units of an 18-decimal token
    d = _evaluate(
        assessment=_assessment(repay_amount=eighteen_dp_repay),
        debt_decimals=18, debt_price_base=USDC_PRICE_BASE,
    )
    assert _check(d, "repay_bounded").passed


def test_unpriceable_repay_blocks_rather_than_passing_by_accident() -> None:
    d = _evaluate(debt_price_base=0)
    assert "repay_bounded" in d.blocking


def test_cost_above_borrower_mandate_blocks() -> None:
    d = _evaluate(params=_params(max_cost_bps=50), assessment=_assessment(est_cost_bps=80))
    assert "cost_within_mandate" in d.blocking


def test_cost_above_operator_ceiling_blocks_even_within_mandate() -> None:
    """The operator's ceiling is not widened by a permissive borrower mandate."""
    d = _evaluate(
        params=_params(max_cost_bps=900),
        assessment=_assessment(est_cost_bps=300),
        limits=PolicyLimits(max_cost_bps_ceiling=200),
    )
    assert "cost_within_mandate" in d.blocking
    assert "operator 200" in _check(d, "cost_within_mandate").detail


def test_cost_exactly_at_the_stricter_cap_passes() -> None:
    d = _evaluate(
        params=_params(max_cost_bps=200), assessment=_assessment(est_cost_bps=200),
        limits=PolicyLimits(max_cost_bps_ceiling=500),
    )
    assert _check(d, "cost_within_mandate").passed


def test_collateral_outside_the_signed_allowlist_blocks() -> None:
    d = _evaluate(assessment=_assessment(collateral_asset=WSTETH))
    assert "collateral_allowlisted" in d.blocking


def test_target_below_the_signed_band_blocks() -> None:
    d = _evaluate(params=_params(hf_target_base_bps=13_000), assessment=_assessment(hf_target=1.25))
    assert "target_in_signed_band" in d.blocking


def test_target_above_the_signed_band_blocks() -> None:
    # The whole band must sit below the target; RiskParams itself rejects max < base.
    d = _evaluate(
        params=_params(hf_target_base_bps=11_600, hf_target_max_bps=12_000),
        assessment=_assessment(hf_target=1.25),
    )
    assert "target_in_signed_band" in d.blocking


@pytest.mark.parametrize("target", [1.25, 1.40])
def test_target_at_either_band_edge_passes(target: float) -> None:
    """Inclusive at both ends, matching the vault's TargetOutOfBand comparison."""
    d = _evaluate(assessment=_assessment(hf_target=target))
    assert _check(d, "target_in_signed_band").passed


# --- Hard blocks -----------------------------------------------------------------------------


def test_paused_breaker_is_a_hard_block() -> None:
    d = _evaluate(metrics=_metrics(breaker_paused=True, breaker_trip_reason="CONSECUTIVE_FAILURES"))
    assert d.allowed is False
    assert d.severity == "hard_block"
    assert "CONSECUTIVE_FAILURES" in _check(d, "breaker_ok").detail


def test_in_flight_borrower_is_a_hard_block() -> None:
    d = _evaluate(metrics=_metrics(in_flight_borrowers=[BORROWER]))
    assert d.severity == "hard_block"
    assert "not_inflight" in d.blocking


def test_another_borrower_in_flight_does_not_block() -> None:
    other = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
    assert _evaluate(metrics=_metrics(in_flight_borrowers=[other])).allowed is True


def test_ordinary_failures_are_soft_blocks() -> None:
    d = _evaluate(assessment=_assessment(viable=False))
    assert d.allowed is False
    assert d.severity == "soft_block"


def test_hard_block_dominates_a_concurrent_soft_block() -> None:
    """A paused keeper is the more urgent fact, so it must not be masked by a soft failure."""
    d = _evaluate(assessment=_assessment(viable=False), metrics=_metrics(breaker_paused=True))
    assert d.severity == "hard_block"


# --- Rate limits -----------------------------------------------------------------------------


def test_per_borrower_rate_limit_blocks() -> None:
    d = _evaluate(recent_proposals_borrower=3, limits=PolicyLimits(max_proposals_per_borrower_per_hour=3))
    assert "rate_limit_borrower" in d.blocking


def test_per_borrower_rate_limit_allows_up_to_the_cap() -> None:
    d = _evaluate(recent_proposals_borrower=2, limits=PolicyLimits(max_proposals_per_borrower_per_hour=3))
    assert _check(d, "rate_limit_borrower").passed


def test_global_rate_limit_blocks_independently() -> None:
    d = _evaluate(
        recent_proposals_borrower=0, recent_proposals_global=12,
        limits=PolicyLimits(max_proposals_global_per_hour=12),
    )
    assert "rate_limit_global" in d.blocking
    assert _check(d, "rate_limit_borrower").passed


# --- repay_value_base ------------------------------------------------------------------------


def test_repay_value_base_converts_usdc_to_usd_base() -> None:
    # 400 USDC (6dp) at $1.00 -> 400 USD at 8dp
    assert repay_value_base(400_000_000, debt_price_base=100_000_000, debt_decimals=6) == 400 * 10**8


def test_repay_value_base_handles_eighteen_decimals() -> None:
    # 2 units of an 18dp token at $2,500 -> 5,000 USD at 8dp
    assert repay_value_base(
        2 * 10**18, debt_price_base=2_500 * 10**8, debt_decimals=18
    ) == 5_000 * 10**8


@pytest.mark.parametrize("price,decimals", [(0, 6), (-1, 6), (100_000_000, -1)])
def test_repay_value_base_returns_zero_for_unusable_inputs(price: int, decimals: int) -> None:
    assert repay_value_base(400_000_000, debt_price_base=price, debt_decimals=decimals) == 0
