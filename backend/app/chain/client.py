"""Async web3 provider with primary+fallback RPC and reconnect (NFR-3).

``ChainClient`` owns one or more :class:`AsyncWeb3` handles and routes every read through
:meth:`call`, which retries the primary endpoint and then transparently fails over to the
fallback. Contract objects are cached per (address, abi-id) so callers get cheap, typed
``functions.*`` access without re-binding on every request.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final, TypeVar

from eth_utils.address import to_checksum_address
from web3 import AsyncWeb3
from web3.contract.async_contract import AsyncContract

from app.config.settings import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Errors worth failing over / retrying on: transport-level, not contract reverts.
_RETRYABLE: Final = (asyncio.TimeoutError, ConnectionError, OSError)


class ChainClientError(RuntimeError):
    """Raised when every configured RPC endpoint fails a call."""


class ChainClient:
    """Manages RPC connections and exposes retry/failover-wrapped reads."""

    def __init__(
        self,
        primary_url: str | None = None,
        fallback_url: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._urls: list[str] = [u for u in (primary_url or settings.arbitrum_rpc_url,
                                             fallback_url or settings.arbitrum_rpc_url_fallback) if u]
        if not self._urls:
            raise ChainClientError("No RPC URL configured (set ARBITRUM_RPC_URL).")
        self._timeout = timeout if timeout is not None else settings.rpc_timeout_seconds
        self._max_retries = max_retries if max_retries is not None else settings.rpc_max_retries
        self._providers: list[AsyncWeb3[Any]] = [self._build(u) for u in self._urls]
        self._active = 0  # index of the endpoint currently preferred
        self._contracts: dict[tuple[int, str, int], AsyncContract] = {}

    def _build(self, url: str) -> AsyncWeb3[Any]:
        request_kwargs = {"timeout": self._timeout}
        return AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(url, request_kwargs=request_kwargs))

    @property
    def w3(self) -> AsyncWeb3[Any]:
        """The currently-active provider (last one that succeeded)."""
        return self._providers[self._active]

    def contract(self, address: str, abi: list[dict[str, Any]]) -> AsyncContract:
        """Return a cached AsyncContract bound to the active provider."""
        checksum = to_checksum_address(address)
        key = (self._active, checksum, id(abi))
        cached = self._contracts.get(key)
        if cached is None:
            cached = self.w3.eth.contract(address=checksum, abi=abi)
            self._contracts[key] = cached
        return cached

    async def call(self, fn: Callable[[AsyncWeb3[Any]], Awaitable[T]]) -> T:
        """Run ``fn(active_provider)`` with retry on the active endpoint then failover.

        ``fn`` must be idempotent (a read/eth_call). It receives the AsyncWeb3 handle so it
        can bind contracts against whichever endpoint is live after a failover.
        """
        last_exc: Exception | None = None
        for offset in range(len(self._providers)):
            idx = (self._active + offset) % len(self._providers)
            provider = self._providers[idx]
            for attempt in range(1, self._max_retries + 1):
                try:
                    result = await fn(provider)
                    self._active = idx  # stick to the endpoint that just worked
                    return result
                except _RETRYABLE as exc:
                    last_exc = exc
                    logger.warning(
                        "RPC call failed (endpoint=%s attempt=%d/%d): %s",
                        self._urls[idx], attempt, self._max_retries, exc,
                    )
                    await asyncio.sleep(min(0.25 * attempt, 2.0))
                except Exception:  # contract revert / value error: do not failover
                    raise
            logger.error("RPC endpoint exhausted, failing over: %s", self._urls[idx])
        raise ChainClientError(f"All RPC endpoints failed; last error: {last_exc}") from last_exc

    async def chain_id(self) -> int:
        return await self.call(lambda w3: w3.eth.chain_id)

    async def block_number(self) -> int:
        return await self.call(lambda w3: w3.eth.block_number)

    async def is_connected(self) -> bool:
        try:
            await self.chain_id()
            return True
        except Exception:  # noqa: BLE001 - health probe, never raises
            return False


# Lazily-constructed process-wide client (built on first use so importing this module
# never requires RPC config — matters for unit tests and `--help`).
_client: ChainClient | None = None


def get_client() -> ChainClient:
    global _client
    if _client is None:
        _client = ChainClient()
    return _client
