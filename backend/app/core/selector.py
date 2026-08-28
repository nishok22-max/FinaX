"""Collateral selection (FR-5) — rank eligible collaterals and pick the best.

Ranks by (eligible, lowest swap slippage/cost, highest liquidation threshold): a higher ``LT_c``
needs less collateral released and lifts HF more (PRD §7), while lower slippage means a cheaper,
safer swap. The pure ``rank_collaterals`` / ``build_choice`` core is unit-testable; ``CollateralSelector``
wires the Aave/Uniswap/oracle/ERC20 clients to populate the metrics on a fork.

v1 primary path is single WETH collateral → USDC debt; this generalises it for FR-5 without changing
the on-chain contract (the chosen collateral is just an ``executeProtection`` argument).
"""
from __future__ import annotations

from app.chain.aave import AaveClient
from app.chain.erc20 import ERC20Client
from app.chain.oracle import OracleClient
from app.chain.uniswap import UniswapClient
from app.config.arbitrum import AAVE_BASE_CURRENCY_DECIMALS, BPS
from app.core.models import CollateralChoice


def _to_base_value(amount: int, decimals: int, price_base: int) -> int:
    """USD-base (1e8) value of ``amount`` tokens at oracle ``price_base`` (also 1e8)."""
    return int((amount * price_base) // (10**decimals))


def build_choice(
    *,
    collateral_asset: str,
    fee_tier: int,
    amount_in: int,
    collateral_decimals: int,
    collateral_price_base: int,
    out_needed: int,
    debt_decimals: int,
    debt_price_base: int,
    liq_threshold_bps: int,
    has_allowance: bool,
) -> CollateralChoice:
    """Assemble one candidate's cost metrics from a quote + oracle prices (pure)."""
    amount_in_value = _to_base_value(amount_in, collateral_decimals, collateral_price_base)
    out_value = _to_base_value(out_needed, debt_decimals, debt_price_base)
    slippage_bps = ((amount_in_value - out_value) * BPS) // out_value if out_value > 0 else 0
    slippage_bps = max(slippage_bps, 0)
    eligible = has_allowance and amount_in > 0 and out_value > 0
    reason = None if eligible else (
        "no aToken allowance" if not has_allowance else "no quote / zero value"
    )
    return CollateralChoice(
        collateral_asset=collateral_asset,
        fee_tier=fee_tier,
        amount_in=amount_in,
        amount_in_value_base=amount_in_value,
        out_needed_value_base=out_value,
        slippage_cost_bps=int(slippage_bps),
        liq_threshold_bps=liq_threshold_bps,
        has_allowance=has_allowance,
        eligible=eligible,
        reason=reason,
    )


def rank_collaterals(choices: list[CollateralChoice]) -> list[CollateralChoice]:
    """Best-first ordering: eligible first, then cheapest swap, then highest LT."""
    return sorted(
        choices,
        key=lambda c: (not c.eligible, c.slippage_cost_bps, -c.liq_threshold_bps),
    )


class CollateralSelector:
    """Gathers per-collateral metrics from chain and returns the ranked best choice."""

    def __init__(
        self,
        aave: AaveClient,
        uniswap: UniswapClient,
        oracle: OracleClient,
        erc20: ERC20Client,
    ) -> None:
        self._aave = aave
        self._uniswap = uniswap
        self._oracle = oracle
        self._erc20 = erc20

    async def evaluate(
        self,
        *,
        borrower: str,
        vault_address: str,
        debt_asset: str,
        out_needed: int,
        candidate_collaterals: list[str],
    ) -> list[CollateralChoice]:
        """Score each candidate collateral for swapping to ``out_needed`` of the debt asset."""
        debt_reserve = await self._aave.get_reserve_info(debt_asset)
        debt_price = (await self._oracle.get_asset_price(debt_asset)).price

        choices: list[CollateralChoice] = []
        for collateral in candidate_collaterals:
            reserve = await self._aave.get_reserve_info(collateral)
            price = (await self._oracle.get_asset_price(collateral)).price
            fee_tier = _fee_tier(collateral, debt_asset)
            try:
                quote = await self._uniswap.quote_exact_output_single(
                    token_in=collateral, token_out=debt_asset, amount_out=out_needed, fee=fee_tier
                )
                amount_in = quote.amount_in
            except Exception:  # noqa: BLE001 - unquotable pair is simply ineligible
                amount_in = 0
            allowance = await self._erc20.allowance(
                reserve.aToken_address, borrower, vault_address
            )
            choices.append(
                build_choice(
                    collateral_asset=collateral,
                    fee_tier=fee_tier,
                    amount_in=amount_in,
                    collateral_decimals=reserve.decimals,
                    collateral_price_base=price,
                    out_needed=out_needed,
                    debt_decimals=debt_reserve.decimals,
                    debt_price_base=debt_price,
                    liq_threshold_bps=reserve.liq_threshold_bps,
                    has_allowance=0 < amount_in <= allowance,
                )
            )
        return rank_collaterals(choices)

    async def select_best(
        self,
        *,
        borrower: str,
        vault_address: str,
        debt_asset: str,
        out_needed: int,
        candidate_collaterals: list[str],
    ) -> CollateralChoice | None:
        ranked = await self.evaluate(
            borrower=borrower,
            vault_address=vault_address,
            debt_asset=debt_asset,
            out_needed=out_needed,
            candidate_collaterals=candidate_collaterals,
        )
        if ranked and ranked[0].eligible:
            return ranked[0]
        return None


def _fee_tier(token_in: str, token_out: str) -> int:
    from app.config.arbitrum import fee_tier_for

    return fee_tier_for(token_in, token_out)


# Base-currency decimal exposed for callers computing values alongside the selector.
BASE_DECIMALS = AAVE_BASE_CURRENCY_DECIMALS
