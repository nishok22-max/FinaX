"""Shared test fixtures.

Two tiers:
  * Pure-unit tests (models, config) need no chain and always run.
  * Fork-integration tests need ``web3`` importable **and** a reachable RPC (an ``anvil
    --fork-url`` fork, or a live Arbitrum One endpoint). Those requirements are checked here so
    the suite skips cleanly on a machine without them instead of erroring at import.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config.settings import settings


def _web3_importable() -> bool:
    try:
        import web3  # noqa: F401
    except Exception:  # noqa: BLE001 - native/DLL or install issues -> skip fork tests
        return False
    return True


def anvil_bin() -> str | None:
    found = shutil.which("anvil")
    if found:
        return found
    candidate = Path.home() / ".foundry" / "bin" / ("anvil.exe" if os.name == "nt" else "anvil")
    return str(candidate) if candidate.exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def fork_source() -> str:
    """Endpoint anvil forks FROM — must answer anvil's network-family probe AND serve pinned-block
    state token-free. The official Arbitrum endpoint does both; override with ANVIL_FORK_URL."""
    return os.environ.get("ANVIL_FORK_URL") or "https://arb1.arbitrum.io/rpc"


@pytest.fixture(scope="module")
def anvil_url() -> Iterator[str]:
    """A running ``anvil`` Arbitrum fork (shanghai EVM, pinned recent block). Skips if unavailable."""
    if not _web3_importable():
        pytest.skip("web3 not importable")
    anvil = anvil_bin()
    if not anvil:
        pytest.skip("anvil not found (~/.foundry/bin)")

    import httpx

    src = fork_source()
    try:
        resp = httpx.post(src, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber",
                                     "params": []}, timeout=10)
        fork_block = int(resp.json()["result"], 16) - 3
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not read latest block from fork source: {exc}")

    port = _free_port()
    log = open(Path(os.environ.get("TEMP", ".")) / f"anvil_{port}.log", "w+")  # noqa: SIM115
    # --hardfork shanghai avoids anvil's "Excess blob gas not set" on an Arbitrum fork.
    proc = subprocess.Popen(
        [anvil, "--fork-url", src, "--fork-block-number", str(fork_block),
         "--port", str(port), "--hardfork", "shanghai"],
        stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId",
                                          "params": []}, timeout=2)
                if r.status_code == 200 and "result" in r.json():
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        if not ready:
            log.seek(0)
            pytest.skip(f"anvil fork not ready; log tail:\n{log.read()[-800:]}")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        log.close()


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
