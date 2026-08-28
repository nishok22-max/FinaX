"""Aave V3 Pool client — typed reads and calldata encoders.

Reads (``get_user_account_data``, ``get_reserve_info``) drive the monitor and sizing pipeline;
the encoders (``encode_repay``, ``encode_withdraw``) produce calldata for the Phase 5 simulator's
``eth_call`` dry-runs. The Pool address is resolved once from the ``PoolAddressesProvider`` so a
fork/testnet override flows through automatically.
"""
from __future__ import annotations

from typing import Any, cast

from eth_typing import ChecksumAddress
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3 import AsyncWeb3

from app.chain.client import ChainClient, get_client
from app.config.arbitrum import (
    AAVE_POOL_ABI,
    AAVE_POOL_ADDRESSES_PROVIDER,
    POOL_ADDRESSES_PROVIDER_ABI,
    TOKEN_DECIMALS,
    VARIABLE_RATE_MODE,
)
from app.core.models import ReserveInfo, UserAccountData

# Aave V3 ReserveConfigurationMap bit layout (packed into the `data` word).
_LTV_MASK = (1 << 16) - 1
_LT_BIT_SHIFT = 16
_LT_MASK = (1 << 16) - 1
_BONUS_BIT_SHIFT = 32
_BONUS_MASK = (1 << 16) - 1
_DECIMALS_BIT_SHIFT = 48
_DECIMALS_MASK = (1 << 8) - 1


class AaveClient:
    """Read/encode wrapper around the Aave V3 Pool."""

    def __init__(self, client: ChainClient | None = None) -> None:
        self._c = client or get_client()
        self._pool_address: ChecksumAddress | None = None

    async def pool_address(self) -> ChecksumAddress:
        """Resolve and cache the Pool address from the PoolAddressesProvider."""
        if self._pool_address is None:

            async def _resolve(w3: AsyncWeb3) -> str:
                provider = w3.eth.contract(
                    address=AAVE_POOL_ADDRESSES_PROVIDER, abi=POOL_ADDRESSES_PROVIDER_ABI
                )
                return cast(str, await provider.functions.getPool().call())

            self._pool_address = to_checksum_address(await self._c.call(_resolve))
        return self._pool_address

    async def _pool(self, w3: AsyncWeb3) -> Any:
        return w3.eth.contract(address=await self.pool_address(), abi=AAVE_POOL_ABI)

    async def get_user_account_data(self, borrower: str) -> UserAccountData:
        user = to_checksum_address(borrower)

        async def _read(w3: AsyncWeb3) -> tuple[int, int, int, int, int, int]:
            pool = await self._pool(w3)
            return cast(
                "tuple[int, int, int, int, int, int]",
                await pool.functions.getUserAccountData(user).call(),
            )

        (coll, debt, avail, lt, ltv, hf) = await self._c.call(_read)
        return UserAccountData(
            total_collateral_base=coll,
            total_debt_base=debt,
            available_borrows_base=avail,
            liquidation_threshold_bps=lt,
            ltv_bps=ltv,
            health_factor=hf,
        )

    async def get_reserve_info(self, asset: str) -> ReserveInfo:
        """aToken address, variable-debt token, decimals, and liquidation threshold for `asset`."""
        token = to_checksum_address(asset)

        async def _read(w3: AsyncWeb3) -> tuple[Any, ...]:
            pool = await self._pool(w3)
            return cast("tuple[Any, ...]", await pool.functions.getReserveData(token).call())

        data = await self._c.call(_read)
        config_data = int(data[0][0])  # configuration.data (packed)
        decimals_on_chain = (config_data >> _DECIMALS_BIT_SHIFT) & _DECIMALS_MASK
        liq_threshold_bps = (config_data >> _LT_BIT_SHIFT) & _LT_MASK
        bonus_raw = (config_data >> _BONUS_BIT_SHIFT) & _BONUS_MASK
        # Aave stores the bonus as a multiplier over 100% (e.g. 10500 = 5% bonus); 0 = unset.
        liq_bonus_bps = bonus_raw - 10_000 if bonus_raw > 10_000 else 0
        return ReserveInfo(
            asset=token,
            aToken_address=to_checksum_address(data[8]),
            variable_debt_token_address=to_checksum_address(data[10]),
            decimals=decimals_on_chain or TOKEN_DECIMALS.get(token, 18),
            liq_threshold_bps=liq_threshold_bps,
            liq_bonus_bps=liq_bonus_bps,
        )

    def encode_repay(self, asset: str, amount: int, on_behalf_of: str) -> HexBytes:
        """Calldata for ``Pool.repay(asset, amount, VARIABLE, onBehalfOf)`` (permissionless)."""
        contract = self._c.w3.eth.contract(abi=AAVE_POOL_ABI)
        return HexBytes(
            contract.encode_abi(
                "repay",
                args=[
                    to_checksum_address(asset),
                    amount,
                    VARIABLE_RATE_MODE,
                    to_checksum_address(on_behalf_of),
                ],
            )
        )

    def encode_withdraw(self, asset: str, amount: int, to: str) -> HexBytes:
        """Calldata for ``Pool.withdraw(asset, amount, to)``."""
        contract = self._c.w3.eth.contract(abi=AAVE_POOL_ABI)
        return HexBytes(
            contract.encode_abi(
                "withdraw",
                args=[to_checksum_address(asset), amount, to_checksum_address(to)],
            )
        )
