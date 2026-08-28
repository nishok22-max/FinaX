"""Transaction submission — the keeper signs and broadcasts ``executeProtection``.

Key separation (critical security property): the **borrower's key never touches the backend** —
the borrower signs ``RiskParams`` client-side and that signature is passed in. The keeper key
(loaded from env/KMS) only *triggers*, bounded by the signed params, and signs the outer tx with
``eth_account``. An optional private/MEV-protected relay hook is provided; the default broadcasts
publicly. After the receipt resolves, HF is re-read to label the outcome RESTORED / REVERTED.
"""
from __future__ import annotations

import logging
from typing import Any, cast

from eth_account import Account
from eth_utils.address import to_checksum_address
from web3 import AsyncWeb3

from app.chain.aave import AaveClient
from app.chain.client import ChainClient
from app.config.arbitrum import vault_abi
from app.core.models import RescuePlan, RiskParams, SubmissionResult
from app.core.state import PositionState

logger = logging.getLogger(__name__)


class SubmitterError(RuntimeError):
    """Raised when the keeper signer is not configured."""


class Submitter:
    """Signs with the keeper key and broadcasts the rescue transaction."""

    def __init__(
        self,
        client: ChainClient,
        aave: AaveClient,
        *,
        vault_address: str,
        keeper_private_key: str,
    ) -> None:
        if not keeper_private_key:
            raise SubmitterError("KEEPER_PRIVATE_KEY not configured")
        self._c = client
        self._aave = aave
        self._vault = to_checksum_address(vault_address)
        self._account = Account.from_key(keeper_private_key)

    @property
    def keeper_address(self) -> str:
        return cast(str, self._account.address)

    async def submit(
        self,
        plan: RescuePlan,
        params: RiskParams,
        signature: str,
        *,
        repay_amount: int,
        amount_in_maximum: int,
    ) -> SubmissionResult:
        sig = bytes.fromhex(signature.removeprefix("0x"))

        async def _send(w3: AsyncWeb3[Any]) -> tuple[str, int, int]:
            vault = w3.eth.contract(address=self._vault, abi=vault_abi())
            fn = vault.functions.executeProtection(
                params.to_solidity_tuple(), sig,
                to_checksum_address(plan.debt_asset), repay_amount,
                to_checksum_address(plan.collateral_asset), amount_in_maximum,
                plan.fee_tier, plan.hf_target_bps,
            )
            nonce = await w3.eth.get_transaction_count(self._account.address)
            chain_id = await w3.eth.chain_id
            tx = await fn.build_transaction(
                {"from": self._account.address, "nonce": nonce, "chainId": chain_id}
            )
            signed = self._account.sign_transaction(tx)
            tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            return tx_hash.hex(), int(receipt["status"]), int(receipt["gasUsed"])

        tx_hash, status, gas_used = await self._c.call(_send)
        tx_hex = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"

        hf_after: float | None = None
        if status == 1:
            account = await self._aave.get_user_account_data(plan.borrower)
            hf_after = account.hf
            state = PositionState.RESTORED
        else:
            state = PositionState.REVERTED

        logger.info(
            "submission borrower=%s tx=%s status=%d state=%s hf_after=%s",
            plan.borrower, tx_hex, status, state.value, hf_after,
        )
        return SubmissionResult(
            tx_hash=tx_hex, status=status, state=state, hf_after=hf_after, gas_used=gas_used
        )
