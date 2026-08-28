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

from enum import Enum

from eth_utils.address import to_checksum_address
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.arbitrum import AAVE_BASE_CURRENCY_DECIMALS, BPS, WAD

# --- Enums ---------------------------------------------------------------------------------


class PositionState(str, Enum):
    """Lifecycle of a monitored position (full transitions wired in Phase 5)."""

    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    ASSESSING = "ASSESSING"
    DECLINED = "DECLINED"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    RESTORED = "RESTORED"
    REVERTED = "REVERTED"


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
