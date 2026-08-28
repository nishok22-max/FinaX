"""Unit tests for collateral selection (FR-5) — pure ranking + choice construction."""
from __future__ import annotations

from app.core.selector import build_choice, rank_collaterals

WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
WSTETH = "0x5979D7b546E38E414F7E9822514be443A4800529"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"

USDC_PRICE = 10**8          # $1
WETH_PRICE = 3_000 * 10**8  # $3000
USDC_DECIMALS = 6
WETH_DECIMALS = 18


def _weth_choice(amount_in_weth_wei: int, *, allowance: bool = True, lt_bps: int = 8_000):
    # out_needed = 1,000 USDC.
    return build_choice(
        collateral_asset=WETH,
        fee_tier=500,
        amount_in=amount_in_weth_wei,
        collateral_decimals=WETH_DECIMALS,
        collateral_price_base=WETH_PRICE,
        out_needed=1_000 * 10**USDC_DECIMALS,
        debt_decimals=USDC_DECIMALS,
        debt_price_base=USDC_PRICE,
        liq_threshold_bps=lt_bps,
        has_allowance=allowance,
    )


def test_build_choice_computes_slippage_bps() -> None:
    # ~0.3355 WETH == ~$1006.5 to buy $1000 USDC -> ~65 bps of cost.
    c = _weth_choice(335_500_000_000_000_000)  # 0.3355 WETH in wei
    assert c.eligible
    assert 50 <= c.slippage_cost_bps <= 80


def test_choice_without_allowance_is_ineligible() -> None:
    c = _weth_choice(333_333_333_333_333_333, allowance=False)
    assert not c.eligible
    assert c.reason == "no aToken allowance"


def test_ranking_prefers_lower_slippage() -> None:
    cheap = _weth_choice(333_500_000_000_000_000)   # ~0.05% over
    pricey = _weth_choice(340_000_000_000_000_000)   # ~2% over
    ranked = rank_collaterals([pricey, cheap])
    assert ranked[0] is cheap


def test_ranking_puts_eligible_before_ineligible() -> None:
    good = _weth_choice(334_000_000_000_000_000, allowance=True)
    bad = _weth_choice(333_000_000_000_000_000, allowance=False)
    ranked = rank_collaterals([bad, good])
    assert ranked[0] is good
    assert not ranked[-1].eligible


def test_ranking_tiebreaks_on_higher_lt() -> None:
    # Same swap input/slippage; higher LT should rank first.
    low_lt = _weth_choice(334_000_000_000_000_000, lt_bps=7_000)
    high_lt = _weth_choice(334_000_000_000_000_000, lt_bps=8_500)
    ranked = rank_collaterals([low_lt, high_lt])
    assert ranked[0] is high_lt
