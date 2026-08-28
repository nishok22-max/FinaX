"""Unit tests for the economic-viability gate (FR-11)."""
from __future__ import annotations

from app.core.viability import assess_viability

BASE = 10**8  # $1 in base currency


def test_viable_when_penalty_exceeds_cost() -> None:
    # $1000 repay, 5% liq bonus -> $50 protected; small swap+gas cost.
    r = assess_viability(
        repay_value_base=1_000 * BASE,
        liq_bonus_bps=500,
        swap_cost_base=2 * BASE,      # $2 slippage+fee
        gas_cost_base=BASE // 2,      # $0.50
    )
    assert r.viable
    assert r.value_protected_base == 50 * BASE
    assert r.cost_base < r.value_protected_base


def test_not_viable_when_cost_dominates() -> None:
    # Tiny position: $20 repay, 5% bonus -> $1 protected; $2 swap cost dwarfs it.
    r = assess_viability(
        repay_value_base=20 * BASE,
        liq_bonus_bps=500,
        swap_cost_base=2 * BASE,
        gas_cost_base=BASE,
    )
    assert not r.viable
    assert r.reason is not None


def test_est_cost_bps_reflects_cost_fraction() -> None:
    # cost = premium(0.05% of 1000 = 0.5) + swap(5) + gas(0.5) = $6 on $1000 -> 60 bps.
    r = assess_viability(
        repay_value_base=1_000 * BASE,
        liq_bonus_bps=800,
        swap_cost_base=5 * BASE,
        gas_cost_base=BASE // 2,
    )
    assert r.est_cost_bps == 60


def test_flash_premium_included_in_cost() -> None:
    # Zero swap/gas: cost must still be the 0.05% flash premium.
    r = assess_viability(
        repay_value_base=1_000 * BASE, liq_bonus_bps=500, swap_cost_base=0, gas_cost_base=0
    )
    assert r.cost_base == (1_000 * BASE * 5) // 10_000  # 0.05%
