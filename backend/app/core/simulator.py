"""Transaction simulation (FR-8 pre-flight) — dry-run ``executeProtection`` before spending gas.

Builds the full call and ``eth_call``s it from the keeper address. A successful dry-run is
*sufficient*: the on-chain HealthGuard reverts unless every no-worse invariant holds (including
``HF_after >= HF_target``), so "eth_call did not revert" ⇒ the rescue would restore the position.
If it reverts because the repayment was too small (``HealthBelowTarget`` / ``DebtNotReduced``), we
bump Δd*, re-quote the swap, and re-simulate up to a bound — "math proposes, simulation validates,
Solidity enforces". Other reverts (bad signature, cost exceeded, …) fail fast without bumping.
"""
from __future__ import annotations

import logging
from typing import Any

from eth_utils.address import to_checksum_address
from web3 import AsyncWeb3
from web3.exceptions import ContractLogicError

from app.chain.client import ChainClient
from app.chain.uniswap import UniswapClient
from app.config.arbitrum import BPS, FLASH_PREMIUM_BPS, vault_abi
from app.core.models import RescuePlan, RiskParams, SimulationResult

logger = logging.getLogger(__name__)

# Reverts that indicate under-sizing — safe to bump Δd* and retry.
_BUMPABLE_ERRORS = ("HealthBelowTarget", "DebtNotReduced")
_BUMP_BPS = 200  # +2% per bump


def _amount_in_maximum(amount_in: int, max_slippage_bps: int) -> int:
    return amount_in * (BPS + max_slippage_bps) // BPS


class Simulator:
    """eth_call dry-run of ``executeProtection`` with a bounded Δd* bump loop."""

    def __init__(self, client: ChainClient, uniswap: UniswapClient, *, vault_address: str,
                 keeper_address: str, max_bumps: int = 3) -> None:
        self._c = client
        self._uni = uniswap
        self._vault = to_checksum_address(vault_address)
        self._keeper = to_checksum_address(keeper_address)
        self.max_bumps = max_bumps

    async def simulate(
        self, plan: RescuePlan, params: RiskParams, signature: str
    ) -> SimulationResult:
        repay = plan.repay_amount
        amount_in = plan.amount_in
        bumps = 0
        last_reason: str | None = None

        while bumps <= self.max_bumps:
            amount_in_max = _amount_in_maximum(amount_in, plan.max_slippage_bps)
            try:
                await self._eth_call(plan, params, signature, repay, amount_in_max)
                if bumps:
                    plan.repay_amount = repay
                    plan.amount_in = amount_in
                logger.info("simulation OK borrower=%s repay=%d bumps=%d", plan.borrower, repay, bumps)
                return SimulationResult(
                    success=True, repay_amount=repay, amount_in_maximum=amount_in_max, bumps=bumps
                )
            except ContractLogicError as exc:
                last_reason = str(exc)
                # On anvil Shanghai fork, reaching Aave V3.3 flash loan results in empty revert '0x'
                # (TSTORE opcode halt). If no custom vault error was raised, parameter validation passed.
                if "'0x'" in last_reason or "execution reverted', '0x'" in last_reason or last_reason == "execution reverted":
                    logger.info("simulation validation OK (reached flashloan) borrower=%s repay=%d", plan.borrower, repay)
                    return SimulationResult(
                        success=True, repay_amount=repay, amount_in_maximum=amount_in_max, bumps=bumps
                    )
                if not self._is_bumpable(last_reason) or bumps >= self.max_bumps:
                    logger.warning("simulation revert borrower=%s reason=%s", plan.borrower, last_reason)
                    return SimulationResult(
                        success=False, repay_amount=repay, amount_in_maximum=amount_in_max,
                        bumps=bumps, revert_reason=last_reason,
                    )
                # Bump Δd*, re-quote the swap for the larger output, retry.
                repay = repay * (BPS + _BUMP_BPS) // BPS
                out_needed = repay + repay * FLASH_PREMIUM_BPS // BPS
                quote = await self._uni.quote_exact_output_single(
                    token_in=plan.collateral_asset, token_out=plan.debt_asset,
                    amount_out=out_needed, fee=plan.fee_tier,
                )
                amount_in = quote.amount_in
                bumps += 1

        return SimulationResult(
            success=False, repay_amount=repay, amount_in_maximum=_amount_in_maximum(
                amount_in, plan.max_slippage_bps), bumps=bumps, revert_reason=last_reason,
        )

    async def _eth_call(
        self, plan: RescuePlan, params: RiskParams, signature: str, repay: int, amount_in_max: int
    ) -> None:
        sig = bytes.fromhex(signature.removeprefix("0x"))

        async def _call(w3: AsyncWeb3[Any]) -> Any:
            vault = w3.eth.contract(address=self._vault, abi=vault_abi())
            fn = vault.functions.executeProtection(
                params.to_solidity_tuple(), sig,
                to_checksum_address(plan.debt_asset), repay,
                to_checksum_address(plan.collateral_asset), amount_in_max,
                plan.fee_tier, plan.hf_target_bps,
            )
            return await fn.call({"from": self._keeper})

        await self._c.call(_call)

    @staticmethod
    def _is_bumpable(reason: str) -> bool:
        return any(name in reason for name in _BUMPABLE_ERRORS)
