"""Pydantic data models — the Python mirror of the on-chain ABI plus typed read results.

Two families:
  * **Wire models** (`RiskParams`, `ProtectRequest`, `AssessmentResponse`) mirror the Solidity
    struct and the FastAPI request/response schemas (architecture §8). ``RiskParams`` field
    order is load-bearing: :meth:`RiskParams.to_solidity_tuple` must match the struct member
    order in ``LiquidationShieldVault.RiskParams`` for ABI encoding and EIP-712 hashing.
  * **Read models** (`UserAccountData`, `ReserveInfo`, `Quote`, `OraclePrice`) are typed results
    returned by the chain clients, with unit-aware convenience properties so the decision
    pipeline never juggles raw WAD/bps/base-currency integers.
"""
from __future__ import annotations

from eth_utils.address import to_checksum_address
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.arbitrum import AAVE_BASE_CURRENCY_DECIMALS, BPS, WAD
from app.core.state import (
    PositionState as PositionState,  # noqa: PLC0414  (explicit re-export; state.py is canonical)
)

# --- Wire models (mirror on-chain ABI / FastAPI schemas) -----------------------------------


class RiskParams(BaseModel):
    """Borrower-signed risk policy — mirrors ``LiquidationShieldVault.RiskParams`` (EIP-712).

    Field order matches the Solidity struct exactly; do not reorder without updating the
    contract's ``RISK_PARAMS_TYPEHASH`` and :meth:`to_solidity_tuple`.
    """

    model_config = ConfigDict(populate_by_name=True)

    borrower: str
    hf_trigger_bps: int = Field(alias="hfTriggerBps", ge=0)
    hf_target_base_bps: int = Field(alias="hfTargetBaseBps", ge=0)
    vol_coeff_k: int = Field(alias="volCoeffK", ge=0)
    hf_target_max_bps: int = Field(alias="hfTargetMaxBps", ge=0)
    max_slippage_bps: int = Field(alias="maxSlippageBps", ge=0, le=BPS)
    max_cost_bps: int = Field(alias="maxCostBps", ge=0, le=BPS)
    allowed_collaterals: list[str] = Field(alias="allowedCollaterals")
    nonce: int = Field(ge=0)
    deadline: int = Field(ge=0)

    @field_validator("borrower")
    @classmethod
    def _checksum_borrower(cls, v: str) -> str:
        return to_checksum_address(v)

    @field_validator("allowed_collaterals")
    @classmethod
    def _checksum_collaterals(cls, v: list[str]) -> list[str]:
        return [to_checksum_address(a) for a in v]

    def to_solidity_tuple(
        self,
    ) -> tuple[str, int, int, int, int, int, int, list[str], int, int]:
        """Positional tuple for web3 ABI encoding of the ``RiskParams`` struct."""
        return (
            self.borrower,
            self.hf_trigger_bps,
            self.hf_target_base_bps,
            self.vol_coeff_k,
            self.hf_target_max_bps,
            self.max_slippage_bps,
            self.max_cost_bps,
            self.allowed_collaterals,
            self.nonce,
            self.deadline,
        )

    def eip712_message(self) -> dict[str, object]:
        """EIP-712 ``message`` payload (camelCase keys, matching the on-chain typehash)."""
        return {
            "borrower": self.borrower,
            "hfTriggerBps": self.hf_trigger_bps,
            "hfTargetBaseBps": self.hf_target_base_bps,
            "volCoeffK": self.vol_coeff_k,
            "hfTargetMaxBps": self.hf_target_max_bps,
            "maxSlippageBps": self.max_slippage_bps,
            "maxCostBps": self.max_cost_bps,
            "allowedCollaterals": self.allowed_collaterals,
            "nonce": self.nonce,
            "deadline": self.deadline,
        }


class ProtectRequest(BaseModel):
    """Body of ``POST /positions/{borrower}/protect`` (architecture §8)."""

    params: RiskParams
    signature: str  # borrower EIP-712 signature (0x-hex)


class AssessmentResponse(BaseModel):
    """Result of ``GET /positions/{borrower}/assessment`` (architecture §8)."""

    hf: float
    hf_target: float
    repay_amount: int
    collateral_asset: str
    est_cost_bps: int
    viable: bool
    reason: str | None = None


# --- Read models (typed chain-client results) ----------------------------------------------


class UserAccountData(BaseModel):
    """Decoded ``IPool.getUserAccountData`` — Aave's account-level risk snapshot.

    ``*_base`` values are in the oracle base currency (USD, 8 decimals on Arbitrum);
    ``liquidation_threshold`` / ``ltv`` are in basis points; ``health_factor`` is WAD (1e18),
    and equals ``type(uint256).max`` when the account has no debt.
    """

    total_collateral_base: int
    total_debt_base: int
    available_borrows_base: int
    liquidation_threshold_bps: int
    ltv_bps: int
    health_factor: int

    @property
    def hf(self) -> float:
        """Health factor as a float (WAD -> human). ``inf`` when there is no debt."""
        if self.total_debt_base == 0:
            return float("inf")
        return self.health_factor / WAD

    @property
    def has_debt(self) -> bool:
        return self.total_debt_base > 0

    @property
    def collateral_usd(self) -> float:
        return self.total_collateral_base / (10**AAVE_BASE_CURRENCY_DECIMALS)

    @property
    def debt_usd(self) -> float:
        return self.total_debt_base / (10**AAVE_BASE_CURRENCY_DECIMALS)


class ReserveInfo(BaseModel):
    """Per-asset reserve data needed downstream: aToken address + liquidation params.

    ``aToken_address`` drives the collateral-allowance precondition (FR-15); ``liq_threshold_bps``
    feeds the ``Δd*`` sizing formula (FR-3); ``liq_bonus_bps`` (excess over 100%) feeds the
    viability model's ``ValueProtected`` (FR-11). All decoded from the packed reserve config.
    """

    asset: str
    aToken_address: str
    variable_debt_token_address: str
    decimals: int
    liq_threshold_bps: int
    liq_bonus_bps: int = 0  # excess over 10000 (e.g. 500 = 5% liquidation bonus)

    @property
    def liq_threshold(self) -> float:
        return self.liq_threshold_bps / BPS

    @property
    def liq_bonus(self) -> float:
        return self.liq_bonus_bps / BPS


class Quote(BaseModel):
    """Uniswap V3 ``QuoterV2.quoteExactOutputSingle`` result."""

    token_in: str
    token_out: str
    fee: int
    amount_out: int  # desired output (exact)
    amount_in: int  # required input for that output
    sqrt_price_x96_after: int
    initialized_ticks_crossed: int
    gas_estimate: int


class OraclePrice(BaseModel):
    """A price reading with freshness metadata (Aave oracle or Chainlink feed)."""

    asset: str
    price: int  # raw integer at `decimals`
    decimals: int
    updated_at: int  # unix seconds (0 when the source exposes no timestamp)
    stale: bool = False

    @property
    def price_float(self) -> float:
        return float(self.price / (10**self.decimals))


# --- Phase 4 decision-pipeline results -----------------------------------------------------


class RiskSignal(BaseModel):
    """Output of the risk model (FR-2, FR-10): volatility, breach probability, dynamic target."""

    sigma: float  # realized volatility of the window (stdev of periodic returns)
    breach_probability: float  # P(HF < 1.0 within the execution window), in [0, 1]
    hf_target_bps: int  # dynamic target within the borrower's signed band

    @property
    def hf_target(self) -> float:
        return self.hf_target_bps / BPS


class SizingResult(BaseModel):
    """Output of the sizing model (FR-3): candidate minimum repayment to reach the target."""

    repay_amount: int  # debt-token units (decimals-aware, rounded UP) — a candidate
    delta_base: int  # intermediate USD-base delta (1e8) — for on-chain parity checks
    hf_target_bps: int
    feasible: bool  # False when the denominator is non-positive (target unreachable via repay)
    reason: str | None = None


class CollateralChoice(BaseModel):
    """A ranked collateral candidate (FR-5): its swap cost and resulting-HF metrics."""

    collateral_asset: str
    fee_tier: int
    amount_in: int  # collateral units required for the exact-output swap (QuoterV2)
    amount_in_value_base: int  # USD-base value of amount_in (1e8)
    out_needed_value_base: int  # USD-base value of debt produced (1e8)
    slippage_cost_bps: int  # (valueIn - valueOut) / valueOut, in bps
    liq_threshold_bps: int
    has_allowance: bool
    eligible: bool
    reason: str | None = None


class ViabilityResult(BaseModel):
    """Output of the economic-viability gate (FR-11)."""

    value_protected_base: int  # USD-base (1e8): liquidation penalty avoided
    cost_base: int  # USD-base (1e8): flash premium + swap cost + gas
    est_cost_bps: int  # cost as bps of debt protected
    viable: bool
    reason: str | None = None


class PositionSnapshot(BaseModel):
    """A single monitor poll (FR-1): account data plus the classified lifecycle state."""

    borrower: str
    account: UserAccountData
    state: PositionState
    hf: float
    hf_trigger_bps: int


# --- Phase 5 service / simulation / submission ---------------------------------------------


class ProtectResponse(BaseModel):
    """Result of a protect request: the assessment, the decision, and (if submitted) the tx."""

    borrower: str
    state: PositionState
    submitted: bool
    tx_hash: str | None = None
    assessment: AssessmentResponse | None = None
    reason: str | None = None


class RescuePlan(BaseModel):
    """Everything the simulator/submitter need to build the ``executeProtection`` tx."""

    borrower: str
    debt_asset: str
    repay_amount: int          # Δd* candidate (bumped in-place by the simulator)
    collateral_asset: str
    fee_tier: int
    amount_in: int             # quoted collateral input for the current out_needed
    hf_target_bps: int
    max_slippage_bps: int


class SimulationResult(BaseModel):
    """Output of the ``eth_call`` dry-run of ``executeProtection`` (FR-8 pre-flight)."""

    success: bool
    repay_amount: int  # final (possibly bumped) Δd*
    amount_in_maximum: int
    hf_after: float | None = None
    bumps: int = 0
    revert_reason: str | None = None


class SubmissionResult(BaseModel):
    """Output of signing + broadcasting the tx and awaiting the receipt."""

    tx_hash: str
    status: int  # 1 = success, 0 = reverted
    state: PositionState  # RESTORED or REVERTED
    hf_after: float | None = None
    gas_used: int | None = None


class KeeperConfig(BaseModel):
    """Bounded, operator-tunable runtime config exposed via ``GET`` / ``PUT /config``."""

    poll_interval_seconds: int = Field(ge=1, le=3600)
    breaker_max_consecutive_failures: int = Field(ge=1, le=20)
    inflight_cooldown_seconds: int = Field(ge=0, le=3600)
    max_simulation_bumps: int = Field(ge=0, le=10)
    autonomous_enabled: bool = True


class MetricsSnapshot(BaseModel):
    """Observability snapshot for ``GET /metrics`` (NFR-5, FR-17)."""

    breaker_paused: bool
    breaker_consecutive_failures: int
    breaker_trip_reason: str | None
    in_flight_borrowers: list[str]
    registered_positions: int
    counters: dict[str, int]
    states: dict[str, str]
