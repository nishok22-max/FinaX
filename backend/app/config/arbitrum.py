"""Arbitrum One address book, ABIs, fee tiers, and token metadata.

Single source of truth for everything the chain clients bind to. Addresses default to the
pinned Arbitrum One deployment (mirrors ``contracts/addresses.json``) but every field that
can differ on a fork/testnet is overridable via ``settings`` so the same code drives the
mainnet-fork demo and a real deployment.

ABIs are the *minimal subset* actually called by the backend, hand-mirrored from the Solidity
interfaces in ``contracts/src/interfaces``. The full vault ABI is loaded from the Foundry build
export in ``app/abi/LiquidationShieldVault.json`` so the encoder can never drift from the
deployed contract.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from eth_utils.address import to_checksum_address

from app.config.settings import settings

# --- Address book (Arbitrum One, chainId 42161) --------------------------------------------

AAVE_POOL_ADDRESSES_PROVIDER: Final = to_checksum_address(settings.pool_addresses_provider)
UNISWAP_SWAP_ROUTER: Final = to_checksum_address(settings.swap_router)
UNISWAP_QUOTER_V2: Final = to_checksum_address(settings.quoter_v2)

# Token underlyings (pinned; used for decimals/symbol lookups and the collateral allow-list).
TOKENS: Final[dict[str, str]] = {
    "WETH": to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),
    "USDC": to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
    "USDC_e": to_checksum_address("0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8"),
    "wstETH": to_checksum_address("0x5979D7b546E38E414F7E9822514be443A4800529"),
}
# Reverse map for logging (address -> symbol).
TOKEN_SYMBOLS: Final[dict[str, str]] = {addr: sym for sym, addr in TOKENS.items()}

# Static decimals for the v1 asset set (avoids an RPC round-trip on the hot path; the ERC20
# client can still read decimals live for assets not listed here).
TOKEN_DECIMALS: Final[dict[str, int]] = {
    TOKENS["WETH"]: 18,
    TOKENS["USDC"]: 6,
    TOKENS["USDC_e"]: 6,
    TOKENS["wstETH"]: 18,
}

CHAINLINK_FEEDS: Final[dict[str, str]] = {
    "ETH_USD": to_checksum_address(settings.chainlink_eth_usd),
    "USDC_USD": to_checksum_address(settings.chainlink_usdc_usd),
}

# Uniswap V3 fee tier (in hundredths of a bip) for the v1 primary route WETH<->USDC.
FEE_TIERS: Final[dict[tuple[str, str], int]] = {
    (TOKENS["WETH"], TOKENS["USDC"]): 500,
    (TOKENS["USDC"], TOKENS["WETH"]): 500,
}
DEFAULT_FEE_TIER: Final = 500

# --- Scaling constants (mirror HealthMath.sol / Aave conventions) --------------------------

WAD: Final = 10**18  # Aave healthFactor scale
RAY: Final = 10**27
BPS: Final = 10**4
BPS_TO_WAD: Final = 10**14  # bps (1e4) -> WAD (1e18)
AAVE_BASE_CURRENCY_DECIMALS: Final = 8  # getUserAccountData *Base values & oracle price scale
VARIABLE_RATE_MODE: Final = 2  # Aave V3 variable interest-rate mode

# --- Economic constants (sizing / viability) -----------------------------------------------

FLASH_PREMIUM_BPS: Final = 5  # Aave V3 flashLoanSimple premium = 0.05%
# Bundled cost factor `f` in the Δd* denominator: flash premium + DEX fee + slippage headroom.
# Matches the 1% used on-chain in SizingParity.t.sol (`onePlusF = 1.01e18`) for numeric parity.
DEFAULT_BUNDLED_COST_BPS: Final = 100  # 1.00%
HF_LIQUIDATION_BOUNDARY: Final = 1.0  # HF = 1.0 is the liquidation boundary
# Nominal gas cost for the assessment's viability gate, in USD base currency (1e8).
# Arbitrum L2 gas is small; the Phase 5 submitter replaces this with a live gas-oracle estimate.
DEFAULT_GAS_COST_BASE: Final = 50_000_000  # ~$0.50


def fee_tier_for(token_in: str, token_out: str) -> int:
    """Best-known Uniswap V3 fee tier for a pair, defaulting to the 0.05% pool."""
    key = (to_checksum_address(token_in), to_checksum_address(token_out))
    return FEE_TIERS.get(key, DEFAULT_FEE_TIER)


# --- ABIs (minimal subsets, mirrored from contracts/src/interfaces) ------------------------

AAVE_POOL_ABI: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "name": "getUserAccountData",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "getReserveData",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "configuration", "type": "tuple",
                     "components": [{"name": "data", "type": "uint256"}]},
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
                    {"name": "isolationModeTotalDebt", "type": "uint128"},
                ],
            }
        ],
    },
    {
        "type": "function",
        "name": "repay",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "withdraw",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

POOL_ADDRESSES_PROVIDER_ABI: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "name": "getPool",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "getPriceOracle",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]

AAVE_ORACLE_ABI: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "name": "getAssetPrice",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "BASE_CURRENCY_UNIT",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

QUOTER_V2_ABI: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "name": "quoteExactOutputSingle",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    }
]

SWAP_ROUTER_ABI: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "name": "exactOutputSingle",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountOut", "type": "uint256"},
                    {"name": "amountInMaximum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountIn", "type": "uint256"}],
    }
]

# Chainlink AggregatorV3Interface subset.
CHAINLINK_AGGREGATOR_ABI: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "name": "latestRoundData",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
    },
    {
        "type": "function",
        "name": "decimals",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]

# ERC20 + Aave aToken (allowance/permit precondition checks).
ERC20_ABI: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "name": "decimals",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "type": "function",
        "name": "symbol",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "allowance",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


@lru_cache(maxsize=1)
def vault_abi() -> list[dict[str, Any]]:
    """Full vault ABI, loaded from the Foundry build export (never hand-edited)."""
    path = Path(__file__).resolve().parent.parent / "abi" / "LiquidationShieldVault.json"
    with path.open(encoding="utf-8") as fh:
        abi: list[dict[str, Any]] = json.load(fh)
    return abi
