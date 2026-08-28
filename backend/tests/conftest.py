"""Shared test fixtures.

Two tiers:
  * Pure-unit tests (models, config) need no chain and always run.
  * Fork-integration tests need ``web3`` importable **and** a reachable RPC (an ``anvil
    --fork-url`` fork, or a live Arbitrum One endpoint). Those requirements are checked here so
    the suite skips cleanly on a machine without them instead of erroring at import.
"""
from __future__ import annotations

import os

import pytest


from app.config.settings import settings


def _web3_importable() -> bool:
    try:
        import web3  # noqa: F401
    except Exception:  # noqa: BLE001 - native/DLL or install issues -> skip fork tests
        return False
    return True


@pytest.fixture(scope="session")
def rpc_url() -> str:
    url = os.environ.get("ARBITRUM_RPC_URL") or settings.arbitrum_rpc_url
    if not url:
        pytest.skip("ARBITRUM_RPC_URL not set — fork-integration test skipped.")
    return url


@pytest.fixture(scope="session")
def chain_client(rpc_url: str):  # type: ignore[no-untyped-def]
    if not _web3_importable():
        pytest.skip("web3 not importable in this environment — fork-integration test skipped.")
    from app.chain.client import ChainClient

    fallback = os.environ.get("ARBITRUM_RPC_URL_FALLBACK") or settings.arbitrum_rpc_url_fallback
    return ChainClient(primary_url=rpc_url, fallback_url=fallback)
