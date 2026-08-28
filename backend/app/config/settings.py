"""Backend configuration loaded from environment / .env (pydantic-settings).

Phase 0 booted the service with the minimal set. Phase 3 adds the fields the typed
chain clients need: fork pinning, RPC timeouts, oracle staleness, and the token /
Chainlink address book (defaulted to Arbitrum One, overridable for a fork/testnet).
Risk-threshold / gas-policy fields for the decision pipeline arrive in Phase 4+.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Chain / RPC ------------------------------------------------------
    chain_id: int = Field(default=42161, alias="CHAIN_ID")
    arbitrum_rpc_url: str = Field(default="", alias="ARBITRUM_RPC_URL")
    arbitrum_rpc_url_fallback: str = Field(default="", alias="ARBITRUM_RPC_URL_FALLBACK")
    fork_block: int = Field(default=0, alias="FORK_BLOCK")  # 0 = latest (no pin)
    rpc_timeout_seconds: float = Field(default=20.0, alias="RPC_TIMEOUT_SECONDS")
    rpc_max_retries: int = Field(default=3, alias="RPC_MAX_RETRIES")

    poll_interval_seconds: int = Field(default=12, alias="POLL_INTERVAL_SECONDS")

    # --- Core protocol addresses -----------------------------------------
    vault_address: str = Field(default="", alias="VAULT_ADDRESS")
    pool_addresses_provider: str = Field(
        default="0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", alias="POOL_ADDRESSES_PROVIDER"
    )
    swap_router: str = Field(default="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45", alias="SWAP_ROUTER")
    quoter_v2: str = Field(default="0x61fFE014bA17989E743c5F6cB21bF9697530B21e", alias="QUOTER_V2")

    # --- Chainlink feeds (cross-check / freshness; Aave oracle is authoritative on-chain) ---
    chainlink_eth_usd: str = Field(
        default="0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612", alias="CHAINLINK_ETH_USD"
    )
    chainlink_usdc_usd: str = Field(
        default="0x50834F3163758fcC1Df9973b6e91f0F0F0434aD3", alias="CHAINLINK_USDC_USD"
    )
    oracle_max_staleness_seconds: int = Field(default=3600, alias="ORACLE_MAX_STALENESS_SECONDS")

    # --- Signer / breaker -------------------------------------------------
    keeper_private_key: str = Field(default="", alias="KEEPER_PRIVATE_KEY")
    breaker_max_consecutive_failures: int = Field(default=3, alias="BREAKER_MAX_CONSECUTIVE_FAILURES")


settings = Settings()
