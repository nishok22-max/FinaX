"""Unit tests for Δd* sizing (FR-3) — numeric parity with SizingParity.t.sol and correctness.

The correctness check reconstructs HF after applying the sized repayment (Δc = Δd·(1+f)) and
asserts it lands at/above target without large overshoot — the Python side of the cross-layer
parity described in the implementation plan §11.
"""
from __future__ import annotations

from app.config.arbitrum import BPS_TO_WAD, WAD
from app.core.sizing import size_repay

USDC_PRICE_BASE = 10**8  # $1.00 in 8-decimal base currency
USDC_DECIMALS = 6


def _reference_delta_base(
    collateral_base: int, debt_base: int, lt_bps: int, target_bps: int, f_bps: int
) -> int:
    """Independent reimplementation of SizingParity.t.sol::_sizeRepay's integer core."""
    target_wad = target_bps * BPS_TO_WAD
    lt_wad = lt_bps * BPS_TO_WAD
    one_plus_f = WAD + f_bps * BPS_TO_WAD
    num = target_wad * debt_base - collateral_base * lt_wad
    denom = target_wad - (one_plus_f * lt_wad) // WAD
    return num // denom


def _hf_after(collateral_base: int, debt_base: int, lt_bps: int, delta_base: int, f_bps: int) -> float:
    """HF' = (C − Δd·(1+f))·LT / (D − Δd), in float."""
    lt = lt_bps / 10_000
    dc = delta_base * (10_000 + f_bps) // 10_000
    c_after = collateral_base - dc
    d_after = debt_base - delta_base
    return (c_after * lt) / d_after


def test_delta_base_matches_solidity_reference() -> None:
    collat = 6_000 * 10**8   # $6000 collateral
    debt = 4_000 * 10**8     # $4000 debt  -> HF_before = 0.8*6000/4000 = 1.20
    lt_bps = 8_000
    target_bps = 13_000      # 1.30
    r = size_repay(
        collateral_base=collat, debt_base=debt, lt_bps=lt_bps, target_bps=target_bps,
        debt_price_base=USDC_PRICE_BASE, debt_decimals=USDC_DECIMALS,
    )
    assert r.feasible
    assert r.delta_base == _reference_delta_base(collat, debt, lt_bps, target_bps, 100)


def test_sized_repay_reaches_target_not_far_over() -> None:
    collat = 6_000 * 10**8
    debt = 4_000 * 10**8
    lt_bps = 8_000
    target_bps = 13_000
    r = size_repay(
        collateral_base=collat, debt_base=debt, lt_bps=lt_bps, target_bps=target_bps,
        debt_price_base=USDC_PRICE_BASE, debt_decimals=USDC_DECIMALS,
    )
    # Convert the rounded-UP token amount back to base value and reconstruct HF.
    delta_used_base = r.repay_amount * USDC_PRICE_BASE // 10**USDC_DECIMALS
    hf_after = _hf_after(collat, debt, lt_bps, delta_used_base, 100)
    target = target_bps / 10_000
    assert hf_after >= target - 1e-6            # rounded up: never short of target
    assert hf_after <= target * 1.02            # and no massive overshoot


def test_no_repay_when_already_above_target() -> None:
    # HF_before = 0.8*10000/4000 = 2.0, target 1.30 -> nothing to do.
    r = size_repay(
        collateral_base=10_000 * 10**8, debt_base=4_000 * 10**8, lt_bps=8_000,
        target_bps=13_000, debt_price_base=USDC_PRICE_BASE, debt_decimals=USDC_DECIMALS,
    )
    assert r.feasible
    assert r.repay_amount == 0


def test_infeasible_when_denominator_nonpositive() -> None:
    # target below (1+f)*LT makes the denominator <= 0 -> unreachable via repayment.
    r = size_repay(
        collateral_base=6_000 * 10**8, debt_base=4_000 * 10**8, lt_bps=9_500,
        target_bps=9_000, debt_price_base=USDC_PRICE_BASE, debt_decimals=USDC_DECIMALS,
    )
    assert not r.feasible


def test_higher_target_needs_more_repay() -> None:
    base = {
        "collateral_base": 6_000 * 10**8,
        "debt_base": 4_000 * 10**8,
        "lt_bps": 8_000,
        "debt_price_base": USDC_PRICE_BASE,
        "debt_decimals": USDC_DECIMALS,
    }
    low = size_repay(target_bps=12_500, **base)
    high = size_repay(target_bps=13_500, **base)
    assert high.repay_amount > low.repay_amount


def test_repay_rounds_up_in_token_units() -> None:
    r = size_repay(
        collateral_base=6_000 * 10**8, debt_base=4_000 * 10**8, lt_bps=8_000,
        target_bps=13_000, debt_price_base=USDC_PRICE_BASE, debt_decimals=USDC_DECIMALS,
    )
    floor_tokens = r.delta_base * 10**USDC_DECIMALS // USDC_PRICE_BASE
    assert r.repay_amount >= floor_tokens  # rounded up, never below the floor conversion
