"""Price clients — Aave V3 oracle (authoritative) and Chainlink feeds (freshness cross-check).

The Aave oracle is what the vault's on-chain cost bound reads, so sizing/viability price at the
same source (``get_asset_price``). Chainlink ``latestRoundData`` is read separately with a
staleness gate (``get_chainlink_price``) to feed the circuit breaker's stale-oracle trip (FR-17).
"""
from __future__ import annotations

from typing import cast

from eth_typing import ChecksumAddress
from eth_utils import to_checksum_address
from web3 import AsyncWeb3

from app.chain.client import ChainClient, get_client
from app.config.arbitrum import (
    AAVE_BASE_CURRENCY_DECIMALS,
    AAVE_ORACLE_ABI,
    AAVE_POOL_ADDRESSES_PROVIDER,
    CHAINLINK_AGGREGATOR_ABI,
    CHAINLINK_FEEDS,
    POOL_ADDRESSES_PROVIDER_ABI,
)
from app.config.settings import settings
from app.core.models import OraclePrice


class OracleClient:
    """Reads asset prices from the Aave oracle and Chainlink feeds with staleness checks."""

    def __init__(self, client: ChainClient | None = None) -> None:
        self._c = client or get_client()
        self._oracle_address: ChecksumAddress | None = None

    async def oracle_address(self) -> ChecksumAddress:
        """Resolve and cache the Aave price oracle from the PoolAddressesProvider."""
        if self._oracle_address is None:

            async def _resolve(w3: AsyncWeb3) -> str:
                provider = w3.eth.contract(
                    address=AAVE_POOL_ADDRESSES_PROVIDER, abi=POOL_ADDRESSES_PROVIDER_ABI
                )
                return cast(str, await provider.functions.getPriceOracle().call())

            self._oracle_address = to_checksum_address(await self._c.call(_resolve))
        return self._oracle_address

    async def get_asset_price(self, asset: str) -> OraclePrice:
        """Aave oracle price for ``asset`` (USD, 8 decimals on Arbitrum). No timestamp exposed."""
        token = to_checksum_address(asset)

        async def _read(w3: AsyncWeb3) -> int:
            oracle = w3.eth.contract(address=await self.oracle_address(), abi=AAVE_ORACLE_ABI)
            return cast(int, await oracle.functions.getAssetPrice(token).call())

        price = await self._c.call(_read)
        return OraclePrice(
            asset=token,
            price=price,
            decimals=AAVE_BASE_CURRENCY_DECIMALS,
            updated_at=0,
            stale=False,
        )

    async def get_chainlink_price(self, feed_key: str) -> OraclePrice:
        """Chainlink ``latestRoundData`` for a named feed (e.g. ``"ETH_USD"``) with staleness gate."""
        feed_address = to_checksum_address(CHAINLINK_FEEDS[feed_key])

        async def _read(w3: AsyncWeb3) -> tuple[tuple[int, int, int, int, int], int, int]:
            feed = w3.eth.contract(address=feed_address, abi=CHAINLINK_AGGREGATOR_ABI)
            round_data = cast(
                "tuple[int, int, int, int, int]",
                await feed.functions.latestRoundData().call(),
            )
            decimals = cast(int, await feed.functions.decimals().call())
            latest_block = await w3.eth.get_block("latest")
            return round_data, decimals, int(latest_block["timestamp"])

        (round_data, decimals, now) = await self._c.call(_read)
        answer = int(round_data[1])
        updated_at = int(round_data[3])
        age = now - updated_at
        stale = answer <= 0 or age > settings.oracle_max_staleness_seconds
        return OraclePrice(
            asset=feed_key,
            price=max(answer, 0),
            decimals=decimals,
            updated_at=updated_at,
            stale=stale,
        )
