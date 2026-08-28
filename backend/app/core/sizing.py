"""Minimum-effective sizing (FR-3) — the closed-form Δd* candidate.

Replicates the **exact integer arithmetic** of ``SizingParity.t.sol::_sizeRepay`` so the Python
candidate matches the on-chain reference bit-for-bit (Sprint 0 Gate B / §11 cross-layer parity):

    num       = target_wad · D_base − C_base · LT_wad          # base · 1e18
    denom     = target_wad − (1+f)_wad · LT_wad / 1e18         # wad
    Δd_base   = num // denom                                    # USD base (1e8)
    repay_tok = Δd_base · 10^debt_decimals // debt_price_base   # debt-token units

The result is a **candidate**: math proposes, the Phase 5 simulator validates (bump + re-sim),
and the Solidity HealthGuard enforces. We round the token amount UP (safe direction) and expose
``delta_base`` so tests can assert parity against the contract.
"""
from __future__ import annotations

from app.config.arbitrum import BPS_TO_WAD, DEFAULT_BUNDLED_COST_BPS, WAD
from app.core.models import SizingResult


def size_repay(
    *,
    collateral_base: int,
    debt_base: int,
    lt_bps: int,
    target_bps: int,
    debt_price_base: int,
    debt_decimals: int,
    bundled_cost_bps: int = DEFAULT_BUNDLED_COST_BPS,
    overshoot_bps: int = 0,
) -> SizingResult:
    """Candidate Δd* (in debt-token units) to lift HF to ``target_bps``.

    Args mirror ``getUserAccountData`` / oracle units:
      * ``collateral_base`` / ``debt_base``: USD base currency, 8 decimals (``totalCollateralBase`` /
        ``totalDebtBase``).
      * ``lt_bps``: the account's ``currentLiquidationThreshold`` in bps.
      * ``target_bps``: dynamic HF target in bps (must exceed current HF to be reachable).
      * ``debt_price_base``: Aave-oracle price of the debt asset (USD, 8 decimals).
      * ``bundled_cost_bps``: ``f`` — flash premium + DEX fee + slippage headroom (default 1%).
      * ``overshoot_bps``: optional safety over-repay applied to the token amount (the simulator
        normally owns the bump; default 0 keeps this the pure minimum).
    """
    target_wad = target_bps * BPS_TO_WAD
    lt_wad = lt_bps * BPS_TO_WAD
    one_plus_f_wad = WAD + bundled_cost_bps * BPS_TO_WAD

    num = target_wad * debt_base - collateral_base * lt_wad
    denom = target_wad - (one_plus_f_wad * lt_wad) // WAD

    if denom <= 0:
        return SizingResult(
            repay_amount=0, delta_base=0, hf_target_bps=target_bps, feasible=False,
            reason="denominator <= 0: target unreachable via repayment at this LT/cost",
        )
    if num <= 0:
        # HF already at/above target — no repayment needed.
        return SizingResult(
            repay_amount=0, delta_base=0, hf_target_bps=target_bps, feasible=True,
            reason="already at/above target: no repayment required",
        )

    delta_base = num // denom  # USD base (1e8), floor — matches Solidity
    if debt_price_base <= 0:
        return SizingResult(
            repay_amount=0, delta_base=delta_base, hf_target_bps=target_bps, feasible=False,
            reason="debt price unavailable",
        )

    scale = 10**debt_decimals
    # Round UP to token units (safe direction) rather than Solidity's floor.
    repay_tokens = -(-(delta_base * scale) // debt_price_base)
    if overshoot_bps:
        repay_tokens = repay_tokens * (10_000 + overshoot_bps) // 10_000

    return SizingResult(
        repay_amount=int(repay_tokens),
        delta_base=int(delta_base),
        hf_target_bps=target_bps,
        feasible=True,
    )
