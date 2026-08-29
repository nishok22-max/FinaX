"""Backend configuration loaded from environment / .env (pydantic-settings).

Phase 0 booted the service with the minimal set. Phase 3 adds the fields the typed
chain clients need: fork pinning, RPC timeouts, oracle staleness, and the token /
Chainlink address book (defaulted to Arbitrum One, overridable for a fork/testnet).
# Risk-threshold / gas-policy fields for the decision pipeline arrive in Phase 4+.
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

    # --- Signer / breaker / worker ---------------------------------------
    keeper_private_key: str = Field(default="", alias="KEEPER_PRIVATE_KEY")
    breaker_max_consecutive_failures: int = Field(default=3, alias="BREAKER_MAX_CONSECUTIVE_FAILURES")
    inflight_cooldown_seconds: int = Field(default=30, alias="INFLIGHT_COOLDOWN_SECONDS")
    max_simulation_bumps: int = Field(default=3, alias="MAX_SIMULATION_BUMPS")
    autonomous_enabled: bool = Field(default=True, alias="AUTONOMOUS_ENABLED")
    worker_enabled: bool = Field(default=True, alias="WORKER_ENABLED")

    # Local-fork demo aids. Off by default so they can never run in a real
    # deployment: when enabled, an unsigned protect request is signed server-side
    # with the well-known anvil mnemonic (see ProtectionService._resolve_demo_signature).
    demo_mode: bool = Field(default=False, alias="DEMO_MODE")

    # --- Agentic layer (FR-18..FR-22) -------------------------------------
    # Every default here reproduces today's behaviour: with AGENT_ENABLED off (or no API key,
    # or the optional `agent` extra uninstalled) the layer is inert, its routes refuse cleanly,
    # and the keeper runs exactly as it does without this code. The layer never widens the
    # borrower's signed mandate - these are the operator's own, stricter ceilings.
    agent_enabled: bool = Field(default=False, alias="AGENT_ENABLED")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    agent_model: str = Field(default="gemini-2.5-flash", alias="AGENT_MODEL")
    agent_temperature: float = Field(default=0.2, alias="AGENT_TEMPERATURE")
    agent_timeout_seconds: float = Field(default=30.0, alias="AGENT_TIMEOUT_SECONDS")
    agent_max_tool_loops: int = Field(default=6, alias="AGENT_MAX_TOOL_LOOPS")
    agent_db_path: str = Field(default=".agent/finax_agent.db", alias="AGENT_DB_PATH")
    agent_chat_history_turns: int = Field(default=20, alias="AGENT_CHAT_HISTORY_TURNS")

    # Policy-gate ceilings (app/agent/policy.py). Applied on top of RiskParams; stricter wins.
    agent_max_repay_fraction: float = Field(default=0.50, alias="AGENT_MAX_REPAY_FRACTION")
    agent_max_cost_bps: int = Field(default=200, alias="AGENT_MAX_COST_BPS")
    agent_min_hf_gap_bps: int = Field(default=25, alias="AGENT_MIN_HF_GAP_BPS")
    agent_proposal_ttl_seconds: int = Field(default=900, alias="AGENT_PROPOSAL_TTL_SECONDS")
    agent_max_proposals_per_hour: int = Field(default=3, alias="AGENT_MAX_PROPOSALS_PER_HOUR")
    agent_max_proposals_global_per_hour: int = Field(
        default=12, alias="AGENT_MAX_PROPOSALS_GLOBAL_PER_HOUR"
    )

    # Let the worker tick drive the crew. Off even when the agent is on: the keeper loop must
    # never wait on a third-party model to decide whether to protect a position.
    agent_crew_on_tick: bool = Field(default=False, alias="AGENT_CREW_ON_TICK")


settings = Settings()
