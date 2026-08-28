"""Economic-viability gate (FR-11 / PRD §8) — proceed only if ValueProtected > InterventionCost.

Pure function over USD-base-currency integers (1e8), so it is deterministic and unit-testable:

    ValueProtected = liquidationBonus · debtLiquidatable
    InterventionCost = flashPremium(0.05%·repay) + swapCost(slippage+fee) + gas

``swap_cost_base`` comes from the selected collateral's quote (``amount_in_value − out_needed_value``),
``gas_cost_base`` from the gas oracle. The on-chain ``maxCostBps`` / ``amountInMaximum`` bounds are the
belt-and-suspenders enforcement; this gate declines early so we never spend gas on a bad rescue.
"""
from __future__ import annotations

from app.config.arbitrum import BPS, FLASH_PREMIUM_BPS
from app.core.models import ViabilityResult


def assess_viability(
    *,
    repay_value_base: int,
    liq_bonus_bps: int,
    swap_cost_base: int,
    gas_cost_base: int,
    debt_liquidatable_base: int | None = None,
) -> ViabilityResult:
    """Decide whether the rescue is economically justified.

    Args (all USD base, 1e8):
      * ``repay_value_base``: value of debt repaid (Δd*), basis for the flash premium and cost bps.
      * ``liq_bonus_bps``: the collateral's liquidation bonus (excess over 100%).
      * ``swap_cost_base``: collateral spent minus debt produced (Uniswap slippage + pool fee).
      * ``gas_cost_base``: estimated gas cost in USD base.
      * ``debt_liquidatable_base``: portion exposed to liquidation penalty; defaults to the repaid
        value (the at-risk slice the rescue neutralises).
    """
    liquidatable = repay_value_base if debt_liquidatable_base is None else debt_liquidatable_base

    value_protected = (liquidatable * liq_bonus_bps) // BPS
    flash_premium = (repay_value_base * FLASH_PREMIUM_BPS) // BPS
    cost = flash_premium + max(swap_cost_base, 0) + max(gas_cost_base, 0)

    est_cost_bps = (cost * BPS) // repay_value_base if repay_value_base > 0 else 0
    viable = value_protected > cost
    reason = None if viable else (
        f"cost {cost} >= value protected {value_protected} "
        f"(premium={flash_premium}, swap={swap_cost_base}, gas={gas_cost_base})"
    )
    return ViabilityResult(
        value_protected_base=value_protected,
        cost_base=cost,
        est_cost_bps=int(est_cost_bps),
        viable=viable,
        reason=reason,
    )
