"""Unit tests for the risk model (FR-2, FR-10)."""
from __future__ import annotations

import math

from app.core.risk import (
    RollingVolatility,
    assess_risk,
    breach_probability,
    dynamic_hf_target_bps,
)


def test_rolling_volatility_zero_for_flat_series() -> None:
    v = RollingVolatility(window=10)
    v.extend([100.0] * 6)
    assert v.sigma() == 0.0


def test_rolling_volatility_positive_for_moving_series() -> None:
    v = RollingVolatility(window=10)
    v.extend([100.0, 102.0, 99.0, 105.0, 98.0])
    assert v.sigma() > 0.0


def test_rolling_volatility_window_truncation() -> None:
    v = RollingVolatility(window=3)
    v.extend([100, 200, 100, 200, 100, 200])  # only last 3 returns retained
    assert len(v.returns()) <= 3


def test_dynamic_target_floor_at_low_vol() -> None:
    # Low/zero sigma -> target sits at the signed base (1.25).
    assert dynamic_hf_target_bps(0.0, base_bps=12_500, max_bps=14_000, k=7_500) == 12_500


def test_dynamic_target_ceiling_at_high_vol() -> None:
    # High sigma -> clamped to the signed ceiling (1.40).
    assert dynamic_hf_target_bps(0.30, base_bps=12_500, max_bps=14_000, k=7_500) == 14_000


def test_dynamic_target_scales_between() -> None:
    low = dynamic_hf_target_bps(0.01, base_bps=12_500, max_bps=14_000, k=7_500)
    high = dynamic_hf_target_bps(0.05, base_bps=12_500, max_bps=14_000, k=7_500)
    assert 12_500 <= low < high <= 14_000


def test_breach_probability_bounds_and_monotonicity() -> None:
    # Healthier position -> lower breach probability.
    p_safe = breach_probability(1.5, 0.05)
    p_risky = breach_probability(1.05, 0.05)
    assert 0.0 <= p_safe <= 1.0
    assert 0.0 <= p_risky <= 1.0
    assert p_risky > p_safe


def test_breach_probability_edge_cases() -> None:
    assert breach_probability(float("inf"), 0.1) == 0.0
    assert breach_probability(1.2, 0.0) == 0.0     # no vol, above boundary
    assert breach_probability(0.9, 0.0) == 1.0     # already below boundary


def test_assess_risk_bundles_signal() -> None:
    sig = assess_risk(1.1, 0.02, base_bps=12_500, max_bps=14_000, k=7_500)
    assert sig.hf_target_bps >= 12_500
    assert 0.0 <= sig.breach_probability <= 1.0
    assert math.isclose(sig.hf_target, sig.hf_target_bps / 10_000)
