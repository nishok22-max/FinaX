"""Seed multiple diverse test wallets on Anvil Arbitrum fork."""
import asyncio
from web3 import AsyncWeb3
from eth_account import Account
import json, re
from pathlib import Path

WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
PROVIDER = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
MNEMONIC = "test test test test test test test test test test test junk"

_WETH_ABI = [
    {"type": "function", "name": "deposit", "stateMutability": "payable", "inputs": [], "outputs": []},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable", "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}]},
]
_POOL_ABI = [
    {"type": "function", "name": "supply", "stateMutability": "nonpayable", "inputs": [{"name": "a", "type": "address"}, {"name": "amt", "type": "uint256"}, {"name": "o", "type": "address"}, {"name": "r", "type": "uint16"}], "outputs": []},
    {"type": "function", "name": "borrow", "stateMutability": "nonpayable", "inputs": [{"name": "a", "type": "address"}, {"name": "amt", "type": "uint256"}, {"name": "m", "type": "uint256"}, {"name": "r", "type": "uint16"}, {"name": "o", "type": "address"}], "outputs": []},
    {"type": "function", "name": "setUserUseReserveAsCollateral", "stateMutability": "nonpayable", "inputs": [{"name": "a", "type": "address"}, {"name": "u", "type": "bool"}], "outputs": []},
    {"type": "function", "name": "getUserAccountData", "stateMutability": "view", "inputs": [{"name": "user", "type": "address"}], "outputs": [{"name": "totalCollateralBase", "type": "uint256"}, {"name": "totalDebtBase", "type": "uint256"}, {"name": "availableBorrowsBase", "type": "uint256"}, {"name": "currentLiquidationThreshold", "type": "uint256"}, {"name": "ltv", "type": "uint256"}, {"name": "healthFactor", "type": "uint256"}]},
    {"type": "function", "name": "getReserveData", "stateMutability": "view", "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"name": "configuration", "type": "tuple", "components": [{"name": "data", "type": "uint256"}]}, {"name": "liquidityIndex", "type": "uint128"}, {"name": "currentLiquidityRate", "type": "uint128"}, {"name": "variableBorrowIndex", "type": "uint128"}, {"name": "currentVariableBorrowRate", "type": "uint128"}, {"name": "currentStableBorrowRate", "type": "uint128"}, {"name": "lastUpdateTimestamp", "type": "uint40"}, {"name": "id", "type": "uint16"}, {"name": "aTokenAddress", "type": "address"}, {"name": "stableDebtTokenAddress", "type": "address"}, {"name": "variableDebtTokenAddress", "type": "address"}, {"name": "interestRateStrategyAddress", "type": "address"}, {"name": "accruedToTreasury", "type": "uint128"}, {"name": "unbacked", "type": "uint128"}, {"name": "isolationModeTotalDebt", "type": "uint128"}]},
]
_PROVIDER_ABI = [
    {"type": "function", "name": "getPool", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "getPriceOracle", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]
_ORACLE_ABI = [
    {"type": "function", "name": "getAssetPrice", "stateMutability": "view", "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]
_ATOKEN_ABI = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable", "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}]},
]

async def seed_all():
    Account.enable_unaudited_hdwallet_features()
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider("http://127.0.0.1:8548"))
    provider = w3.eth.contract(address=AsyncWeb3.to_checksum_address(PROVIDER), abi=_PROVIDER_ABI)
    pool_addr = await provider.functions.getPool().call()
    oracle_addr = await provider.functions.getPriceOracle().call()
    pool = w3.eth.contract(address=pool_addr, abi=_POOL_ABI)
    oracle = w3.eth.contract(address=oracle_addr, abi=_ORACLE_ABI)
    weth = w3.eth.contract(address=AsyncWeb3.to_checksum_address(WETH), abi=_WETH_ABI)

    env_file = Path(__file__).resolve().parent.parent / ".env"
    match = re.search(r"^VAULT_ADDRESS=(.*)$", env_file.read_text(), re.MULTILINE)
    vault_addr = match.group(1).strip() if match else ""

    res_data = await pool.functions.getReserveData(AsyncWeb3.to_checksum_address(WETH)).call()
    aweth = w3.eth.contract(address=res_data[8], abi=_ATOKEN_ABI)

    configs = [
        {"idx": 1, "weth": 5.0,  "target_hf": 1.1400, "label": "Account 1 (Critical 1.14)"},
        {"idx": 2, "weth": 10.0, "target_hf": 1.0950, "label": "Account 2 (Whale Debt 1.09)"},
        {"idx": 3, "weth": 2.5,  "target_hf": 1.0750, "label": "Account 3 (Retail Danger 1.07)"},
        {"idx": 4, "weth": 8.0,  "target_hf": 1.1950, "label": "Account 4 (Watch Zone 1.19)"},
    ]

    for cfg in configs:
        acct = Account.from_mnemonic(MNEMONIC, account_path=f"m/44'/60'/0'/0/{cfg['idx']}")
        addr = acct.address
        uad = await pool.functions.getUserAccountData(addr).call()
        
        # 1. Supply collateral if empty
        if uad[0] == 0:
            supply_wei = w3.to_wei(cfg["weth"], "ether")
            t1 = await weth.functions.deposit().transact({"from": addr, "value": supply_wei})
            await w3.eth.wait_for_transaction_receipt(t1)
            t2 = await weth.functions.approve(pool_addr, 2**256 - 1).transact({"from": addr})
            await w3.eth.wait_for_transaction_receipt(t2)
            t3 = await pool.functions.supply(AsyncWeb3.to_checksum_address(WETH), supply_wei, addr, 0).transact({"from": addr})
            await w3.eth.wait_for_transaction_receipt(t3)
            t4 = await pool.functions.setUserUseReserveAsCollateral(AsyncWeb3.to_checksum_address(WETH), True).transact({"from": addr})
            await w3.eth.wait_for_transaction_receipt(t4)

        # 2. Borrow to target HF
        uad_mid = await pool.functions.getUserAccountData(addr).call()
        col_base, debt_base, _, lt_bps, _, _ = uad_mid
        target_debt_base = int(col_base * (lt_bps / 10000) / cfg["target_hf"])
        shortfall_base = target_debt_base - debt_base
        if shortfall_base > 0:
            usdc_price = await oracle.functions.getAssetPrice(AsyncWeb3.to_checksum_address(USDC)).call()
            borrow_usdc = shortfall_base * 10**6 // usdc_price
            t5 = await pool.functions.borrow(AsyncWeb3.to_checksum_address(USDC), borrow_usdc, 2, 0, addr).transact({"from": addr})
            await w3.eth.wait_for_transaction_receipt(t5)

        # 3. Approve aToken to vault
        if vault_addr:
            t6 = await aweth.functions.approve(AsyncWeb3.to_checksum_address(vault_addr), 2**256 - 1).transact({"from": addr})
            await w3.eth.wait_for_transaction_receipt(t6)

        final_uad = await pool.functions.getUserAccountData(addr).call()
        final_hf = final_uad[5] / 1e18 if final_uad[1] > 0 else 999.0
        print(f"{cfg['label']} ({addr}): Collateral=${final_uad[0]/1e8:,.2f}, Debt=${final_uad[1]/1e8:,.2f}, HF={final_hf:.4f}")

if __name__ == "__main__":
    asyncio.run(seed_all())
