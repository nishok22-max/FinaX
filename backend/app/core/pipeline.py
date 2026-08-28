"""Read-only decision pipeline (Phase 4 exit criterion).

Composes monitor → risk → sizing → selection → viability into an :class:`AssessmentResponse`
for a fork position, with **no simulation and no submission** (those are Phase 5's
``simulator``/``submitter``, wrapped by ``protection_service``). This is exactly what
``GET /positions/{borrower}/assessment`` returns: the dynamic ``HF_target``, the candidate Δd*,
the selected collateral, and the viability verdict.
"""
from __future__ import annotations

import logging

from app.chain.aave import AaveClient
from app.chain.erc20 import ERC20Client
from app.chain.oracle import OracleClient
from app.chain.uniswap import UniswapClient
from app.config.arbitrum import (
    AAVE_BASE_CURRENCY_DECIMALS,
    BPS,
    DEFAULT_GAS_COST_BASE,
    FLASH_PREMIUM_BPS,
)
from app.core.models import AssessmentResponse, RescuePlan, RiskParams
from app.core.monitor import PositionMonitor
from app.core.risk import assess_risk
from app.core.selector import CollateralSelector
from app.core.sizing import size_repay
from app.core.viability import assess_viability

logger = logging.getLogger(__name__)


class AssessmentPipeline:
    """Assembles the decision modules over the chain clients (no side effects on-chain)."""

    def __init__(
        self,
        aave: AaveClient,
        uniswap: UniswapClient,
        oracle: OracleClient,
        erc20: ERC20Client,
        *,
        vault_address: str,
    ) -> None:
        self._aave = aave
        self._oracle = oracle
        self._monitor = PositionMonitor(aave, oracle)
        self._selector = CollateralSelector(aave, uniswap, oracle, erc20)
        self._vault_address = vault_address

    async def evaluate(
        self,
        params: RiskParams,
        *,
        sigma: float | None = None,
        gas_cost_base: int = DEFAULT_GAS_COST_BASE,
    ) -> tuple[AssessmentResponse, RescuePlan | None]:
        """Assess ``params.borrower`` and, when actionable, return the executable rescue plan.

        Returns ``(response, plan)``: ``plan`` is ``None`` on every decline path (no debt, already
        safe, no eligible collateral, not economically viable). ``sigma`` may be supplied (e.g. from
        the monitor's rolling window); when omitted it defaults to 0 (target rests at the floor).
        """
        borrower = params.borrower
        snapshot = await self._monitor.poll_once(borrower, params.hf_trigger_bps)
        account = snapshot.account

        # --- Risk (FR-2, FR-10): dynamic HF target from volatility -----------------------
        # A one-shot assessment has no price history, so σ defaults to 0 (target rests at the
        # signed floor). The Phase 5 worker feeds the monitor's rolling window and passes σ here.
        if sigma is None:
            sigma = self._monitor.sigma_for(params.allowed_collaterals[0]) if params.allowed_collaterals else 0.0
        risk = assess_risk(
            account.hf if account.has_debt else float("inf"),
            sigma,
            base_bps=params.hf_target_base_bps,
            max_bps=params.hf_target_max_bps,
            k=params.vol_coeff_k,
        )

        if not account.has_debt:
            return AssessmentResponse(
                hf=float("inf"), hf_target=risk.hf_target, repay_amount=0,
                collateral_asset="", est_cost_bps=0, viable=False,
                reason="no debt: nothing to protect",
            ), None

        # --- Sizing (FR-3): candidate Δd* to reach the target ----------------------------
        # Debt asset defaults to the v1 path (USDC); a multi-debt position would iterate.
        debt_asset = _infer_debt_asset(params)
        debt_reserve = await self._aave.get_reserve_info(debt_asset)
        debt_price = (await self._oracle.get_asset_price(debt_asset)).price

        sizing = size_repay(
            collateral_base=account.total_collateral_base,
            debt_base=account.total_debt_base,
            lt_bps=account.liquidation_threshold_bps,
            target_bps=risk.hf_target_bps,
            debt_price_base=debt_price,
            debt_decimals=debt_reserve.decimals,
        )
        if not sizing.feasible or sizing.repay_amount == 0:
            return AssessmentResponse(
                hf=account.hf, hf_target=risk.hf_target, repay_amount=0,
                collateral_asset="", est_cost_bps=0, viable=False,
                reason=sizing.reason or "no repayment required",
            ), None

        out_needed = sizing.repay_amount + (sizing.repay_amount * FLASH_PREMIUM_BPS) // BPS

        # --- Selection (FR-5): best collateral to source the swap ------------------------
        best = await self._selector.select_best(
            borrower=borrower,
            vault_address=self._vault_address,
            debt_asset=debt_asset,
            out_needed=out_needed,
            candidate_collaterals=params.allowed_collaterals,
        )
        if best is None:
            return AssessmentResponse(
                hf=account.hf, hf_target=risk.hf_target, repay_amount=sizing.repay_amount,
                collateral_asset="", est_cost_bps=0, viable=False,
                reason="no eligible collateral (missing aToken allowance or unquotable)",
            ), None

        # --- Viability (FR-11): economic gate --------------------------------------------
        collateral_reserve = await self._aave.get_reserve_info(best.collateral_asset)
        swap_cost_base = max(best.amount_in_value_base - best.out_needed_value_base, 0)
        viability = assess_viability(
            repay_value_base=sizing.delta_base,
            liq_bonus_bps=collateral_reserve.liq_bonus_bps,
            swap_cost_base=swap_cost_base,
            gas_cost_base=gas_cost_base,
        )

        logger.info(
            "assessment borrower=%s hf=%.4f target=%.4f repay=%d collateral=%s cost_bps=%d viable=%s",
            borrower, account.hf, risk.hf_target, sizing.repay_amount,
            best.collateral_asset, viability.est_cost_bps, viability.viable,
        )
        response = AssessmentResponse(
            hf=account.hf,
            hf_target=risk.hf_target,
            repay_amount=sizing.repay_amount,
            collateral_asset=best.collateral_asset,
            est_cost_bps=viability.est_cost_bps,
            viable=viability.viable,
            reason=viability.reason,
        )
        # A plan is only actionable when the economics clear the gate.
        plan = None
        if viability.viable:
            plan = RescuePlan(
                borrower=borrower,
                debt_asset=debt_asset,
                repay_amount=sizing.repay_amount,
                collateral_asset=best.collateral_asset,
                fee_tier=best.fee_tier,
                amount_in=best.amount_in,
                hf_target_bps=risk.hf_target_bps,
                max_slippage_bps=params.max_slippage_bps,
            )
        return response, plan

    async def assess(
        self,
        params: RiskParams,
        *,
        sigma: float | None = None,
        gas_cost_base: int = DEFAULT_GAS_COST_BASE,
    ) -> AssessmentResponse:
        """Read-only assessment (returns just the response); see :meth:`evaluate` for the plan."""
        response, _ = await self.evaluate(params, sigma=sigma, gas_cost_base=gas_cost_base)
        return response


def _infer_debt_asset(params: RiskParams) -> str:
    """v1 debt asset is USDC; kept as a hook for multi-debt generalisation."""
    from app.config.arbitrum import TOKENS

    return TOKENS["USDC"]


# Re-exported for symmetry with other modules that reference the base scale.
BASE_DECIMALS = AAVE_BASE_CURRENCY_DECIMALS
