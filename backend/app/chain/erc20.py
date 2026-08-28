"""ERC20 reads — decimals/symbol and the aToken allowance precondition (FR-15).

The vault can only pull collateral if the borrower has granted an aToken allowance (or permit).
``allowance`` lets the backend check that opt-in *before* assessing, so a rescue that would revert
on the collateral step is declined early rather than simulated and thrown away.
"""
from __future__ import annotations

from typing import cast

from eth_utils import to_checksum_address
from web3 import AsyncWeb3

from app.chain.client import ChainClient, get_client
from app.config.arbitrum import ERC20_ABI, TOKEN_DECIMALS


class ERC20Client:
    """Minimal ERC20 read helpers (decimals, symbol, balance, allowance)."""

    def __init__(self, client: ChainClient | None = None) -> None:
        self._c = client or get_client()
        self._decimals: dict[str, int] = dict(TOKEN_DECIMALS)

    async def decimals(self, token: str) -> int:
        addr = to_checksum_address(token)
        if addr in self._decimals:
            return self._decimals[addr]

        async def _read(w3: AsyncWeb3) -> int:
            c = w3.eth.contract(address=addr, abi=ERC20_ABI)
            return cast(int, await c.functions.decimals().call())

        value = await self._c.call(_read)
        self._decimals[addr] = value
        return value

    async def balance_of(self, token: str, account: str) -> int:
        addr = to_checksum_address(token)
        holder = to_checksum_address(account)

        async def _read(w3: AsyncWeb3) -> int:
            c = w3.eth.contract(address=addr, abi=ERC20_ABI)
            return cast(int, await c.functions.balanceOf(holder).call())

        return await self._c.call(_read)

    async def allowance(self, token: str, owner: str, spender: str) -> int:
        addr = to_checksum_address(token)
        owner_a = to_checksum_address(owner)
        spender_a = to_checksum_address(spender)

        async def _read(w3: AsyncWeb3) -> int:
            c = w3.eth.contract(address=addr, abi=ERC20_ABI)
            return cast(int, await c.functions.allowance(owner_a, spender_a).call())

        return await self._c.call(_read)
