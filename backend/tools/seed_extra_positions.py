"""SUPERSEDED — kept for reference only. Do not run.

Replaced by ``seed_all_demo_wallets.py`` (accounts #1-#4) and ``seed_more_wallets.py``
(accounts #5-#8).

Why it was retired
------------------
This script seeds accounts #2, #3 and #4 by *borrow percentage* to roughly HF 1.07 / 1.10 / 1.31.
``seed_all_demo_wallets.py`` seeds the SAME three accounts by *target HF* to 1.095 / 1.075 / 1.195.
Running both compounds the positions and leaves the console showing health factors that match
neither script - the same double-seeding failure ``seed_demo.py`` already documents in its own
docstring.

The whole implementation below is commented out so it cannot execute or be imported by accident.
The canonical order is now:

    python tools/seed_demo.py            # deploys the vault, seeds account #1
    python tools/seed_all_demo_wallets.py  # accounts #1-#4
    python tools/seed_more_wallets.py      # accounts #5-#8
    python tools/sign_demo_mandates.py --write   # EIP-712 mandates for all 8
"""

raise SystemExit(
    "seed_extra_positions.py is superseded - use seed_all_demo_wallets.py + seed_more_wallets.py"
)

# ----------------------------------------------------------------------------------------------
# ORIGINAL IMPLEMENTATION (retained for reference; intentionally inert)
# ----------------------------------------------------------------------------------------------
# from __future__ import annotations
#
# import asyncio
#
# from eth_account import Account
# from web3 import AsyncWeb3
#
# from app.config.settings import settings
#
# MNEMONIC = "test test test test test test test test test test test junk"
# WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
# USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
# PROVIDER = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
#
# _WETH_ABI = [
#     {"type": "function", "name": "deposit", "stateMutability": "payable", "inputs": [], "outputs": []},
#     {"type": "function", "name": "approve", "stateMutability": "nonpayable",
#      "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
#      "outputs": [{"name": "", "type": "bool"}]},
# ]
# _POOL_ABI = [
#     {"type": "function", "name": "supply", "stateMutability": "nonpayable",
#      "inputs": [{"name": "a", "type": "address"}, {"name": "amt", "type": "uint256"},
#                 {"name": "o", "type": "address"}, {"name": "r", "type": "uint16"}], "outputs": []},
#     {"type": "function", "name": "borrow", "stateMutability": "nonpayable",
#      "inputs": [{"name": "a", "type": "address"}, {"name": "amt", "type": "uint256"},
#                 {"name": "m", "type": "uint256"}, {"name": "r", "type": "uint16"},
#                 {"name": "o", "type": "address"}], "outputs": []},
#     {"type": "function", "name": "setUserUseReserveAsCollateral", "stateMutability": "nonpayable",
#      "inputs": [{"name": "a", "type": "address"}, {"name": "u", "type": "bool"}], "outputs": []},
#     {"type": "function", "name": "getUserAccountData", "stateMutability": "view",
#      "inputs": [{"name": "user", "type": "address"}],
#      "outputs": [{"name": "totalCollateralBase", "type": "uint256"},
#                  {"name": "totalDebtBase", "type": "uint256"},
#                  {"name": "availableBorrowsBase", "type": "uint256"},
#                  {"name": "currentLiquidationThreshold", "type": "uint256"},
#                  {"name": "ltv", "type": "uint256"},
#                  {"name": "healthFactor", "type": "uint256"}]},
#     {"type": "function", "name": "getReserveData", "stateMutability": "view",
#      "inputs": [{"name": "asset", "type": "address"}],
#      "outputs": [{"name": "configuration", "type": "tuple", "components": [{"name": "data", "type": "uint256"}]},
#                  {"name": "liquidityIndex", "type": "uint128"},
#                  {"name": "currentLiquidityRate", "type": "uint128"},
#                  {"name": "variableBorrowIndex", "type": "uint128"},
#                  {"name": "currentVariableBorrowRate", "type": "uint128"},
#                  {"name": "currentStableBorrowRate", "type": "uint128"},
#                  {"name": "lastUpdateTimestamp", "type": "uint40"},
#                  {"name": "id", "type": "uint16"},
#                  {"name": "aTokenAddress", "type": "address"},
#                  {"name": "stableDebtTokenAddress", "type": "address"},
#                  {"name": "variableDebtTokenAddress", "type": "address"},
#                  {"name": "interestRateStrategyAddress", "type": "address"},
#                  {"name": "accruedToTreasury", "type": "uint128"},
#                  {"name": "unbacked", "type": "uint128"},
#                  {"name": "isolationModeTotalDebt", "type": "uint128"}]}
# ]
# _PROVIDER_ABI = [
#     {"type": "function", "name": "getPool", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
#     {"type": "function", "name": "getPriceOracle", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
# ]
# _ORACLE_ABI = [
#     {"type": "function", "name": "getAssetPrice", "stateMutability": "view", "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
# ]
# _ATOKEN_ABI = [
#     {"type": "function", "name": "approve", "stateMutability": "nonpayable",
#      "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
#      "outputs": [{"name": "", "type": "bool"}]},
# ]
#
# async def seed():
#     Account.enable_unaudited_hdwallet_features()
#     w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider("http://127.0.0.1:8548"))
#     provider = w3.eth.contract(address=AsyncWeb3.to_checksum_address(PROVIDER), abi=_PROVIDER_ABI)
#     pool_addr = await provider.functions.getPool().call()
#     oracle_addr = await provider.functions.getPriceOracle().call()
#
#     pool = w3.eth.contract(address=pool_addr, abi=_POOL_ABI)
#     weth = w3.eth.contract(address=AsyncWeb3.to_checksum_address(WETH), abi=_WETH_ABI)
#     oracle = w3.eth.contract(address=oracle_addr, abi=_ORACLE_ABI)
#     res_data = await pool.functions.getReserveData(WETH).call()
#     aweth = w3.eth.contract(address=res_data[8], abi=_ATOKEN_ABI)
#     vault_addr = settings.vault_address or "0xd581A6375045dd645aF24e3D6D1bc8864F4fA708"
#
#     # Account 1 is intentionally absent - it belongs to seed_demo.py. Adding it
#     # back here re-creates the double-seeding bug that broke the demo position.
#     configs = [
#         (2, 8.0, 94.5), # Account 2: HF ~ 1.07
#         (3, 3.0, 92),   # Account 3: HF ~ 1.10
#         (4, 6.0, 80),   # Account 4: HF ~ 1.31
#     ]
#
#     for idx, weth_amt, borrow_pct in configs:
#         acct = Account.from_mnemonic(MNEMONIC, account_path=f"m/44'/60'/0'/0/{idx}")
#         supply_wei = w3.to_wei(weth_amt, "ether")
#
#         tx = await weth.functions.deposit().transact({"from": acct.address, "value": supply_wei})
#         await w3.eth.wait_for_transaction_receipt(tx)
#
#         tx = await weth.functions.approve(pool.address, 2**256 - 1).transact({"from": acct.address})
#         await w3.eth.wait_for_transaction_receipt(tx)
#
#         tx = await pool.functions.supply(AsyncWeb3.to_checksum_address(WETH), supply_wei, acct.address, 0).transact({"from": acct.address})
#         await w3.eth.wait_for_transaction_receipt(tx)
#
#         tx = await pool.functions.setUserUseReserveAsCollateral(AsyncWeb3.to_checksum_address(WETH), True).transact({"from": acct.address})
#         await w3.eth.wait_for_transaction_receipt(tx)
#
#         uad = await pool.functions.getUserAccountData(acct.address).call()
#         avail_base = uad[2]
#         usdc_price = await oracle.functions.getAssetPrice(AsyncWeb3.to_checksum_address(USDC)).call()
#         borrow_usdc = int((avail_base * borrow_pct / 100) * 10**6 // usdc_price)
#
#         if borrow_usdc > 0:
#             tx = await pool.functions.borrow(AsyncWeb3.to_checksum_address(USDC), borrow_usdc, 2, 0, acct.address).transact({"from": acct.address})
#             await w3.eth.wait_for_transaction_receipt(tx)
#
#         tx = await aweth.functions.approve(AsyncWeb3.to_checksum_address(vault_addr), 2**256 - 1).transact({"from": acct.address})
#         await w3.eth.wait_for_transaction_receipt(tx)
#
#         uad_final = await pool.functions.getUserAccountData(acct.address).call()
#         collat = uad_final[0] / 1e8
#         debt = uad_final[1] / 1e8
#         hf = uad_final[5] / 1e18 if debt > 0 else float("inf")
#         print(f"Account {idx} ({acct.address}): Collateral=${collat:,.2f}, Debt=${debt:,.2f}, HF={hf:.4f}")
#
# if __name__ == "__main__":
#     asyncio.run(seed())
