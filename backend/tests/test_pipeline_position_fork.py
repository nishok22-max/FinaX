"""Phase 4 exit criterion — the composed pipeline on a REAL fork position.

Starts an ``anvil`` mainnet fork, builds a genuine WETH-collateral / USDC-debt Aave V3 position
(HF ≈ 1.15) using anvil's unlocked dev account, grants the aWETH allowance the vault needs, then
runs ``AssessmentPipeline.assess`` and asserts a correct, viable, sized ``AssessmentResponse``
(repay amount, dynamic target, selected collateral, viability) — with no submission.

Skips cleanly when ``anvil`` or an RPC is unavailable.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
# Arbitrary "vault" address the borrower grants aWETH allowance to (nothing deployed needed for
# a read-only assessment; the selector only checks the allowance amount).
VAULT = "0x00000000000000000000000000000000000000A5"

_WETH_ABI = [
    {"type": "function", "name": "deposit", "stateMutability": "payable", "inputs": [], "outputs": []},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]
_POOL_ABI = [
    {"type": "function", "name": "supply", "stateMutability": "nonpayable",
     "inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "onBehalfOf", "type": "address"}, {"name": "referralCode", "type": "uint16"}],
     "outputs": []},
    {"type": "function", "name": "borrow", "stateMutability": "nonpayable",
     "inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "interestRateMode", "type": "uint256"}, {"name": "referralCode", "type": "uint16"},
                {"name": "onBehalfOf", "type": "address"}],
     "outputs": []},
    {"type": "function", "name": "setUserUseReserveAsCollateral", "stateMutability": "nonpayable",
     "inputs": [{"name": "asset", "type": "address"}, {"name": "use", "type": "bool"}], "outputs": []},
]
_ATOKEN_ABI = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


def _anvil_bin() -> str | None:
    found = shutil.which("anvil")
    if found:
        return found
    candidate = Path.home() / ".foundry" / "bin" / ("anvil.exe" if os.name == "nt" else "anvil")
    return str(candidate) if candidate.exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _fork_source() -> str:
    """RPC anvil forks FROM. Must satisfy both of anvil's needs: answer its network-family probe
    (Alchemy 400s on ``anvil_nodeInfo``) AND serve state at the pinned fork block token-free
    (publicnode 403s on historical state). The official Arbitrum endpoint does both, so it is the
    default; override with ANVIL_FORK_URL."""
    return os.environ.get("ANVIL_FORK_URL") or "https://arb1.arbitrum.io/rpc"


@pytest.fixture(scope="module")
def anvil_url() -> Iterator[str]:
    try:
        import web3  # noqa: F401
    except Exception:  # noqa: BLE001
        pytest.skip("web3 not importable")
    anvil = _anvil_bin()
    if not anvil:
        pytest.skip("anvil not found (~/.foundry/bin)")

    fork_source = _fork_source()
    # Pin a RECENT block: public endpoints serve current state token-free but 403 on archive
    # (historical) requests, so forking at ~latest keeps every state fetch non-archive.
    import httpx

    try:
        resp = httpx.post(fork_source, json={"jsonrpc": "2.0", "id": 1,
                          "method": "eth_blockNumber", "params": []}, timeout=10)
        fork_block = int(resp.json()["result"], 16) - 3
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not read latest block from fork source: {exc}")

    port = _free_port()
    log = open(Path(os.environ.get("TEMP", ".")) / f"anvil_{port}.log", "w+")  # noqa: SIM115
    # --hardfork shanghai avoids anvil's "Excess blob gas not set" on an Arbitrum fork (the forked
    # header lacks Cancun blob-gas fields). We only *call* already-deployed contracts here, so a
    # pre-blob EVM is fine.
    proc = subprocess.Popen(
        [anvil, "--fork-url", fork_source, "--fork-block-number", str(fork_block),
         "--port", str(port), "--hardfork", "shanghai"],
        stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        import httpx

        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:  # anvil exited early
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
            tail = log.read()[-800:]
            pytest.skip(f"anvil fork not ready; log tail:\n{tail}")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        log.close()


async def _mine(w3: Any, tx_hash: Any) -> None:
    await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)


async def test_pipeline_assesses_real_position(anvil_url: str) -> None:
    from web3 import AsyncWeb3

    from app.chain.aave import AaveClient
    from app.chain.client import ChainClient
    from app.chain.erc20 import ERC20Client
    from app.chain.oracle import OracleClient
    from app.chain.uniswap import UniswapClient
    from app.core.models import RiskParams
    from app.core.pipeline import AssessmentPipeline

    client = ChainClient(primary_url=anvil_url, fallback_url="")
    w3 = client.w3
    aave = AaveClient(client)

    accounts = await w3.eth.accounts
    borrower = accounts[0]
    pool_addr = await aave.pool_address()

    weth = w3.eth.contract(address=AsyncWeb3.to_checksum_address(WETH), abi=_WETH_ABI)
    pool = w3.eth.contract(address=pool_addr, abi=_POOL_ABI)

    # 1. Wrap 5 ETH -> WETH and supply as collateral.
    supply_amt = w3.to_wei(5, "ether")
    await _mine(w3, await weth.functions.deposit().transact({"from": borrower, "value": supply_amt}))
    await _mine(w3, await weth.functions.approve(pool_addr, 2**256 - 1).transact({"from": borrower}))
    await _mine(w3, await pool.functions.supply(
        AsyncWeb3.to_checksum_address(WETH), supply_amt, borrower, 0).transact({"from": borrower}))
    await _mine(w3, await pool.functions.setUserUseReserveAsCollateral(
        AsyncWeb3.to_checksum_address(WETH), True).transact({"from": borrower}))

    # 2. Borrow USDC to ~92% of capacity -> HF just above 1.1.
    uad = await aave.get_user_account_data(borrower)
    usdc_price = (await OracleClient(client).get_asset_price(USDC)).price  # 8-dec USD
    borrow_value_base = uad.available_borrows_base * 92 // 100
    borrow_usdc = borrow_value_base * 10**6 // usdc_price
    await _mine(w3, await pool.functions.borrow(
        AsyncWeb3.to_checksum_address(USDC), borrow_usdc, 2, 0, borrower).transact({"from": borrower}))

    # 3. Grant the aWETH allowance the vault needs to pull collateral (FR-15 opt-in).
    reserve = await aave.get_reserve_info(WETH)
    atoken = w3.eth.contract(address=AsyncWeb3.to_checksum_address(reserve.aToken_address),
                             abi=_ATOKEN_ABI)
    await _mine(w3, await atoken.functions.approve(
        AsyncWeb3.to_checksum_address(VAULT), 2**256 - 1).transact({"from": borrower}))

    # 4. Position is live — confirm it is genuinely at risk.
    before = await aave.get_user_account_data(borrower)
    assert before.has_debt
    assert 1.0 < before.hf < 1.25, f"expected an at-risk position, got HF={before.hf}"

    # 5. Run the composed decision pipeline (no submission).
    params = RiskParams(
        borrower=borrower,
        hf_trigger_bps=11_500,
        hf_target_base_bps=12_500,
        vol_coeff_k=7_500,
        hf_target_max_bps=14_000,
        max_slippage_bps=100,
        max_cost_bps=500,
        allowed_collaterals=[WETH],
        nonce=1,
        deadline=2_000_000_000,
    )
    pipe = AssessmentPipeline(
        aave, UniswapClient(client), OracleClient(client), ERC20Client(client),
        vault_address=VAULT,
    )
    result = await pipe.assess(params)
    print(
        f"\n[fork assessment] HF_before={before.hf:.4f} -> target={result.hf_target:.4f} | "
        f"repay={result.repay_amount} ({result.repay_amount / 1e6:.2f} USDC) | "
        f"collateral={result.collateral_asset} | est_cost_bps={result.est_cost_bps} | "
        f"viable={result.viable}"
    )

    # 6. Assert a correct, actionable assessment.
    assert result.hf == pytest.approx(before.hf, rel=1e-6)
    assert result.hf_target >= 1.25
    assert result.hf < result.hf_target                       # below target -> action warranted
    assert result.repay_amount > 0                            # sized Δd*
    assert result.collateral_asset == AsyncWeb3.to_checksum_address(WETH)
    assert result.est_cost_bps > 0
    assert result.viable is True                              # WETH liq-bonus >> intervention cost
    assert result.reason is None
