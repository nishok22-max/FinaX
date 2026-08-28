"""Seed the primary demo borrower on a local anvil Arbitrum fork — idempotent.

Brings account #1 (0x70997970C51812dc3A010C7d01b50e0d17dc79C8) to a target
health factor and approves the vault on aWETH, so the operator console has a
genuinely at-risk Aave V3 position to demonstrate against.

**Safe to re-run.** Earlier revisions supplied a fixed 5 WETH and then borrowed
92% of whatever was currently available, with no check for existing state — so
every run compounded the position (four runs put the borrower at 20 WETH and
HF 1.80, well clear of the trigger, which silently broke the demo). This version
reads the position first and only moves what is missing:

  * vault deployed only when no code exists at the configured address
  * collateral supplied only when the borrower has none
  * debt borrowed only up to the delta needed to reach TARGET_HF
  * a no-op (with a printed summary) when already within tolerance

Nothing here is product logic — this is test-fixture setup that drives Aave
directly. The keeper's own risk/sizing/viability pipeline is untouched.

Usage:
    python tools/seed_demo.py            # seed or top up to TARGET_HF
    python tools/seed_demo.py --status   # report only, change nothing
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

from eth_account import Account
from web3 import AsyncWeb3

WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
PROVIDER = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
SWAP_ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
MNEMONIC = "test test test test test test test test test test test junk"

# The position we want the console to show: just below the 1.15 trigger, so the
# pipeline classifies it at-risk and produces a viable rescue.
TARGET_HF = 1.14
HF_TOLERANCE = 0.02          # already-good band; avoids pointless top-up txs
SUPPLY_WETH = 5              # only ever supplied when collateral is zero

_WETH_ABI = [
    {"type": "function", "name": "deposit", "stateMutability": "payable", "inputs": [], "outputs": []},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]
_POOL_ABI = [
    {"type": "function", "name": "supply", "stateMutability": "nonpayable",
     "inputs": [{"name": "a", "type": "address"}, {"name": "amt", "type": "uint256"},
                {"name": "o", "type": "address"}, {"name": "r", "type": "uint16"}], "outputs": []},
    {"type": "function", "name": "borrow", "stateMutability": "nonpayable",
     "inputs": [{"name": "a", "type": "address"}, {"name": "amt", "type": "uint256"},
                {"name": "m", "type": "uint256"}, {"name": "r", "type": "uint16"},
                {"name": "o", "type": "address"}], "outputs": []},
    {"type": "function", "name": "setUserUseReserveAsCollateral", "stateMutability": "nonpayable",
     "inputs": [{"name": "a", "type": "address"}, {"name": "u", "type": "bool"}], "outputs": []},
    {"type": "function", "name": "getUserAccountData", "stateMutability": "view",
     "inputs": [{"name": "user", "type": "address"}],
     "outputs": [{"name": "totalCollateralBase", "type": "uint256"},
                 {"name": "totalDebtBase", "type": "uint256"},
                 {"name": "availableBorrowsBase", "type": "uint256"},
                 {"name": "currentLiquidationThreshold", "type": "uint256"},
                 {"name": "ltv", "type": "uint256"},
                 {"name": "healthFactor", "type": "uint256"}]},
    {"type": "function", "name": "getReserveData", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}],
     "outputs": [{"name": "configuration", "type": "tuple", "components": [{"name": "data", "type": "uint256"}]},
                 {"name": "liquidityIndex", "type": "uint128"},
                 {"name": "currentLiquidityRate", "type": "uint128"},
                 {"name": "variableBorrowIndex", "type": "uint128"},
                 {"name": "currentVariableBorrowRate", "type": "uint128"},
                 {"name": "currentStableBorrowRate", "type": "uint128"},
                 {"name": "lastUpdateTimestamp", "type": "uint40"},
                 {"name": "id", "type": "uint16"},
                 {"name": "aTokenAddress", "type": "address"},
                 {"name": "stableDebtTokenAddress", "type": "address"},
                 {"name": "variableDebtTokenAddress", "type": "address"},
                 {"name": "interestRateStrategyAddress", "type": "address"},
                 {"name": "accruedToTreasury", "type": "uint128"},
                 {"name": "unbacked", "type": "uint128"},
                 {"name": "isolationModeTotalDebt", "type": "uint128"}]},
]
_PROVIDER_ABI = [
    {"type": "function", "name": "getPool", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "getPriceOracle", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
]
_ORACLE_ABI = [
    {"type": "function", "name": "getAssetPrice", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]
_ATOKEN_ABI = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]


def _hf(account_data: tuple[int, ...]) -> float:
    """Health factor as a float; ``inf`` when the account carries no debt."""
    return float("inf") if account_data[1] == 0 else account_data[5] / 1e18


def _describe(account_data: tuple[int, ...]) -> str:
    hf = _hf(account_data)
    hf_text = "inf" if hf == float("inf") else f"{hf:.4f}"
    return (f"collateral=${account_data[0] / 1e8:,.2f}  "
            f"debt=${account_data[1] / 1e8:,.2f}  HF={hf_text}")


def _env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def _write_vault_address(vault_addr: str) -> None:
    env_file = _env_path()
    if not env_file.exists():
        return
    content = env_file.read_text()
    content = re.sub(r"VAULT_ADDRESS=.*", f"VAULT_ADDRESS={vault_addr}", content)
    env_file.write_text(content)
    print(f"  updated {env_file} with VAULT_ADDRESS={vault_addr}")


def _configured_vault() -> str:
    """Read VAULT_ADDRESS straight from .env (avoids importing app settings)."""
    env_file = _env_path()
    if not env_file.exists():
        return ""
    match = re.search(r"^VAULT_ADDRESS=(.*)$", env_file.read_text(), re.MULTILINE)
    return match.group(1).strip() if match else ""


async def _ensure_vault(w3: AsyncWeb3[Any], deployer: str, keeper_address: str) -> str:
    """Return a live vault address, deploying only if none is already on-chain."""
    existing = _configured_vault()
    if existing:
        code = await w3.eth.get_code(AsyncWeb3.to_checksum_address(existing))
        if code and code != b"\x00":
            print(f"  vault already deployed at {existing} - reusing")
            return existing

    artifact_path = (Path(__file__).resolve().parent.parent.parent
                     / "contracts" / "out" / "LiquidationShieldVault.sol"
                     / "LiquidationShieldVault.json")
    if not artifact_path.exists():
        raise FileNotFoundError(
            f"Vault artifact not found at {artifact_path}. Run `forge build` in contracts/"
        )

    art = json.loads(artifact_path.read_text())
    vault_factory = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    tx = await vault_factory.constructor(
        AsyncWeb3.to_checksum_address(PROVIDER),
        AsyncWeb3.to_checksum_address(SWAP_ROUTER),
        keeper_address,
    ).transact({"from": deployer})
    receipt = await w3.eth.wait_for_transaction_receipt(tx)
    vault_addr = receipt["contractAddress"]
    print(f"  vault deployed at {vault_addr}")
    _write_vault_address(vault_addr)
    return vault_addr


async def seed(rpc_url: str = "http://127.0.0.1:8548", status_only: bool = False) -> None:
    Account.enable_unaudited_hdwallet_features()
    keeper = Account.from_mnemonic(MNEMONIC, account_path="m/44'/60'/0'/0/0")
    borrower_acct = Account.from_mnemonic(MNEMONIC, account_path="m/44'/60'/0'/0/1")
    borrower = borrower_acct.address

    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    accounts = await w3.eth.accounts

    provider = w3.eth.contract(address=AsyncWeb3.to_checksum_address(PROVIDER), abi=_PROVIDER_ABI)
    pool_addr = await provider.functions.getPool().call()
    oracle_addr = await provider.functions.getPriceOracle().call()
    pool = w3.eth.contract(address=pool_addr, abi=_POOL_ABI)
    oracle = w3.eth.contract(address=oracle_addr, abi=_ORACLE_ABI)
    weth = w3.eth.contract(address=AsyncWeb3.to_checksum_address(WETH), abi=_WETH_ABI)

    async def mine(tx_hash: Any) -> Any:
        return await w3.eth.wait_for_transaction_receipt(tx_hash)

    before = await pool.functions.getUserAccountData(borrower).call()
    print(f"Fork {rpc_url}")
    print(f"Borrower {borrower}")
    print(f"  current: {_describe(before)}")

    if status_only:
        return

    vault_addr = await _ensure_vault(w3, accounts[0], keeper.address)

    # 1. Collateral — supplied only when there is none. Never top up, or repeat
    #    runs compound the position (the original defect).
    if before[0] == 0:
        supply_wei = w3.to_wei(SUPPLY_WETH, "ether")
        print(f"  supplying {SUPPLY_WETH} WETH...")
        await mine(await weth.functions.deposit().transact({"from": borrower, "value": supply_wei}))
        await mine(await weth.functions.approve(pool_addr, 2**256 - 1).transact({"from": borrower}))
        await mine(await pool.functions.supply(
            AsyncWeb3.to_checksum_address(WETH), supply_wei, borrower, 0).transact({"from": borrower}))
        await mine(await pool.functions.setUserUseReserveAsCollateral(
            AsyncWeb3.to_checksum_address(WETH), True).transact({"from": borrower}))
    else:
        print(f"  collateral already present (${before[0] / 1e8:,.2f}) - not supplying more")

    # 2. Debt — borrow only the shortfall needed to reach TARGET_HF.
    #    Aave: HF = (collateral * liquidationThreshold) / debt
    #      =>  debt_at_target = collateral * LT / TARGET_HF
    current = await pool.functions.getUserAccountData(borrower).call()
    collateral_base, debt_base, _avail, lt_bps, _ltv, _hf_wad = current
    debt_at_target = int(collateral_base * (lt_bps / 10_000) / TARGET_HF)
    shortfall_base = debt_at_target - debt_base

    hf_now = _hf(current)
    if hf_now != float("inf") and abs(hf_now - TARGET_HF) <= HF_TOLERANCE:
        print(f"  HF {hf_now:.4f} already within {HF_TOLERANCE} of target {TARGET_HF} - no borrow")
    elif shortfall_base <= 0:
        print(f"  debt already at or above target (HF {hf_now:.4f} <= {TARGET_HF}) - no borrow")
    else:
        usdc_price = await oracle.functions.getAssetPrice(
            AsyncWeb3.to_checksum_address(USDC)).call()
        borrow_usdc = shortfall_base * 10**6 // usdc_price
        print(f"  borrowing {borrow_usdc / 1e6:,.2f} USDC to reach HF {TARGET_HF}...")
        await mine(await pool.functions.borrow(
            AsyncWeb3.to_checksum_address(USDC), borrow_usdc, 2, 0, borrower
        ).transact({"from": borrower}))

    # 3. aToken allowance — the borrower's opt-in the vault needs (FR-15).
    reserve = await pool.functions.getReserveData(AsyncWeb3.to_checksum_address(WETH)).call()
    atoken = w3.eth.contract(address=reserve[8], abi=_ATOKEN_ABI)
    allowance = await atoken.functions.allowance(
        borrower, AsyncWeb3.to_checksum_address(vault_addr)).call()
    if allowance == 0:
        print("  approving aWETH to the vault...")
        await mine(await atoken.functions.approve(
            AsyncWeb3.to_checksum_address(vault_addr), 2**256 - 1).transact({"from": borrower}))
    else:
        print("  aWETH allowance already granted - skipping")

    after = await pool.functions.getUserAccountData(borrower).call()
    print(f"  result:  {_describe(after)}")
    hf_final = _hf(after)
    if hf_final == float("inf") or abs(hf_final - TARGET_HF) > HF_TOLERANCE * 3:
        print(f"  WARNING: HF {hf_final} is not near the {TARGET_HF} target - "
              f"the console may not show an at-risk position.")

    _warn_if_worker_will_undo_this()


def _warn_if_worker_will_undo_this() -> None:
    """A running keeper will rescue the freshly-seeded position within one tick.

    The seed puts HF just below the trigger, which is exactly the condition the
    autonomous worker exists to act on: it polls every POLL_INTERVAL_SECONDS,
    protects the borrower, and HF climbs back out of the at-risk band. Observed
    live - a seed to 1.14 had drifted to 1.57 by the next run. Seed with
    autonomy off when you need the at-risk state to persist for a demo.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/config", timeout=3) as resp:
            cfg = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return  # backend not up; nothing to warn about

    if cfg.get("autonomous_enabled"):
        interval = cfg.get("poll_interval_seconds", "?")
        print()
        print(f"  NOTE: the autonomous worker is ENABLED (every {interval}s). It will "
              f"protect this\n        borrower and push HF back above the trigger within "
              f"one tick.")
        print("        To hold the at-risk state for a demo, turn off Autonomous Worker")
        print("        in the System tab (or PUT /config autonomous_enabled=false), then re-seed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", default="http://127.0.0.1:8548")
    parser.add_argument("--status", action="store_true",
                        help="report the current position and exit without sending transactions")
    args = parser.parse_args()
    asyncio.run(seed(args.rpc_url, status_only=args.status))


if __name__ == "__main__":
    main()
