"""Uniswap V3 client — QuoterV2 quotes and SwapRouter calldata.

``quote_exact_output_single`` answers "how much collateral to spend for exactly N debt tokens"
via an ``eth_call`` against QuoterV2 (the function is non-``view`` but returns normally on V2, so
it must never be sent as a transaction). ``encode_exact_output_single`` builds the router calldata
the vault uses on-chain and the simulator dry-runs.
"""
from __future__ import annotations

from typing import Any, cast

from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3 import AsyncWeb3

from app.chain.client import ChainClient, get_client
from app.config.arbitrum import (
    QUOTER_V2_ABI,
    SWAP_ROUTER_ABI,
    UNISWAP_QUOTER_V2,
    UNISWAP_SWAP_ROUTER,
    fee_tier_for,
)
from app.core.models import Quote


class UniswapClient:
    """Quote and calldata helpers for the collateral -> debt exact-output swap."""

    def __init__(self, client: ChainClient | None = None) -> None:
        self._c = client or get_client()

    async def quote_exact_output_single(
        self,
        token_in: str,
        token_out: str,
        amount_out: int,
        fee: int | None = None,
    ) -> Quote:
        """Required ``token_in`` to receive exactly ``amount_out`` of ``token_out``."""
        t_in = to_checksum_address(token_in)
        t_out = to_checksum_address(token_out)
        fee_tier = fee if fee is not None else fee_tier_for(t_in, t_out)
        params = (t_in, t_out, amount_out, fee_tier, 0)  # sqrtPriceLimitX96 = 0

        async def _read(w3: AsyncWeb3) -> tuple[int, int, int, int]:
            quoter = w3.eth.contract(address=UNISWAP_QUOTER_V2, abi=QUOTER_V2_ABI)
            return cast(
                "tuple[int, int, int, int]",
                await quoter.functions.quoteExactOutputSingle(params).call(),
            )

        amount_in, sqrt_after, ticks, gas = await self._c.call(_read)
        return Quote(
            token_in=t_in,
            token_out=t_out,
            fee=fee_tier,
            amount_out=amount_out,
            amount_in=amount_in,
            sqrt_price_x96_after=sqrt_after,
            initialized_ticks_crossed=ticks,
            gas_estimate=gas,
        )

    def encode_exact_output_single(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        recipient: str,
        amount_out: int,
        amount_in_maximum: int,
    ) -> HexBytes:
        """Calldata for ``SwapRouter.exactOutputSingle`` (sqrtPriceLimitX96 = 0)."""
        contract = self._c.w3.eth.contract(abi=SWAP_ROUTER_ABI)
        params: tuple[Any, ...] = (
            to_checksum_address(token_in),
            to_checksum_address(token_out),
            fee,
            to_checksum_address(recipient),
            amount_out,
            amount_in_maximum,
            0,
        )
        return HexBytes(contract.encode_abi("exactOutputSingle", args=[params]))

    @staticmethod
    def swap_router_address() -> str:
        return UNISWAP_SWAP_ROUTER
