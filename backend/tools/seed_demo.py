"""Seed demo script for local Anvil Arbitrum fork.

Deploys LiquidationShieldVault and seeds the demo borrower
(0x70997970C51812dc3A010C7d01b50e0d17dc79C8) with an active Aave V3 position
(HF ~ 1.14), and approves the vault on aWETH.
"""
from __future__ import annotations

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
    {"type": "function", "name": "getPool", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "getPriceOracle", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]
_ORACLE_ABI = [
    {"type": "function", "name": "getAssetPrice", "stateMutability": "view", "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]
_ATOKEN_ABI = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


async def seed(rpc_url: str = "http://127.0.0.1:8548") -> None:
    Account.enable_unaudited_hdwallet_features()
    keeper = Account.from_mnemonic(MNEMONIC, account_path="m/44'/60'/0'/0/0")
    borrower = Account.from_mnemonic(MNEMONIC, account_path="m/44'/60'/0'/0/1")

    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    accounts = await w3.eth.accounts

    provider = w3.eth.contract(address=AsyncWeb3.to_checksum_address(PROVIDER), abi=_PROVIDER_ABI)
    pool_addr = await provider.functions.getPool().call()
    oracle_addr = await provider.functions.getPriceOracle().call()
    oracle = w3.eth.contract(address=oracle_addr, abi=_ORACLE_ABI)

    print(f"Connected to fork at {rpc_url}. Deploying Vault...")
    artifact_path = Path(__file__).resolve().parent.parent.parent / "contracts" / "out" / "LiquidationShieldVault.sol" / "LiquidationShieldVault.json"
    if not artifact_path.exists():
        raise FileNotFoundError(f"Vault artifact not found at {artifact_path}. Run `forge build` in contracts/")

    art = json.loads(artifact_path.read_text())
    Vault = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    deploy_tx = await Vault.constructor(
        AsyncWeb3.to_checksum_address(PROVIDER),
        AsyncWeb3.to_checksum_address(SWAP_ROUTER),
        keeper.address
    ).transact({"from": accounts[0]})
    deploy_rc = await w3.eth.wait_for_transaction_receipt(deploy_tx)
    vault_addr = deploy_rc["contractAddress"]
    print(f"Vault deployed at: {vault_addr}")

    # Build position for borrower
    weth = w3.eth.contract(address=AsyncWeb3.to_checksum_address(WETH), abi=_WETH_ABI)
    pool = w3.eth.contract(address=pool_addr, abi=_POOL_ABI)
    supply_amt = w3.to_wei(5, "ether")

    print(f"Depositing {supply_amt / 1e18} WETH for borrower {borrower.address}...")
    tx1 = await weth.functions.deposit().transact({"from": borrower.address, "value": supply_amt})
    await w3.eth.wait_for_transaction_receipt(tx1)

    tx2 = await weth.functions.approve(pool_addr, 2**256 - 1).transact({"from": borrower.address})
    await w3.eth.wait_for_transaction_receipt(tx2)

    tx3 = await pool.functions.supply(AsyncWeb3.to_checksum_address(WETH), supply_amt, borrower.address, 0).transact({"from": borrower.address})
    await w3.eth.wait_for_transaction_receipt(tx3)

    tx4 = await pool.functions.setUserUseReserveAsCollateral(AsyncWeb3.to_checksum_address(WETH), True).transact({"from": borrower.address})
    await w3.eth.wait_for_transaction_receipt(tx4)

    uad = await pool.functions.getUserAccountData(borrower.address).call()
    available_base = uad[2]
    usdc_price = await oracle.functions.getAssetPrice(AsyncWeb3.to_checksum_address(USDC)).call()
    borrow_usdc = (available_base * 92 // 100) * 10**6 // usdc_price

    print(f"Borrowing {borrow_usdc / 1e6} USDC to establish target HF...")
    tx5 = await pool.functions.borrow(AsyncWeb3.to_checksum_address(USDC), borrow_usdc, 2, 0, borrower.address).transact({"from": borrower.address})
    await w3.eth.wait_for_transaction_receipt(tx5)

    # Approve aWETH to vault
    res_data = await pool.functions.getReserveData(AsyncWeb3.to_checksum_address(WETH)).call()
    aweth_addr = res_data[8]
    atoken = w3.eth.contract(address=aweth_addr, abi=_ATOKEN_ABI)
    tx6 = await atoken.functions.approve(AsyncWeb3.to_checksum_address(vault_addr), 2**256 - 1).transact({"from": borrower.address})
    await w3.eth.wait_for_transaction_receipt(tx6)

    uad_final = await pool.functions.getUserAccountData(borrower.address).call()
    final_hf = uad_final[5] / 1e18
    print(f"Seeding complete! Borrower: {borrower.address}, Collateral: ${uad_final[0] / 1e8:,.2f}, Debt: ${uad_final[1] / 1e8:,.2f}, HF: {final_hf:.4f}")

    # Update backend/.env with the new VAULT_ADDRESS
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        content = env_file.read_text()
        content = re.sub(r"VAULT_ADDRESS=.*", f"VAULT_ADDRESS={vault_addr}", content)
        env_file.write_text(content)
        print(f"Updated {env_file} with VAULT_ADDRESS={vault_addr}")


if __name__ == "__main__":
    asyncio.run(seed())
