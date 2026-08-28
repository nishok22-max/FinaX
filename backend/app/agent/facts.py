"""The FactSheet — the single source of every number the agent layer displays (FR-22).

The rule this module exists to enforce: **the model narrates, Python numbers.** An LLM is free to
say "this position is close to its trigger and a modest repay restores it"; it is never the source
of *how* close or *how* modest. Those come from here, read straight off the backend's own response
models, and the console renders them from :class:`FactSheet` rather than from the prose.

That separation is not paranoia about hallucination alone — it is what keeps the console honest
about provenance. ``frontend/finax.js`` already records the lesson: an earlier revision fabricated
constants and re-implemented sizing in the browser, "rendering guesses in the same visual language
as live data". Every field below therefore carries an entry in :attr:`FactSheet.sources` naming the
backend field it came from, and the UI renders that map.

A bonus falls out of doing this properly. ``RiskSignal.sigma`` and ``RiskSignal.breach_probability``
are computed on *every* assessment today and then discarded — they never reach
``AssessmentResponse``. The FactSheet recovers them, so the agent can explain *why* the dynamic
target moved without anything new being computed.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.config.arbitrum import AAVE_BASE_CURRENCY_DECIMALS, BPS, TOKEN_DECIMALS, TOKEN_SYMBOLS
from app.core.models import AssessmentResponse, PositionSnapshot, RiskSignal

#: Field name -> the backend response field it was read from. Rendered verbatim in the UI's
#: "Sources" footer, so a reader can check any figure against the REST API themselves.
_SOURCES: dict[str, str] = {
    "hf": "AssessmentResponse.hf",
    "hf_target": "AssessmentResponse.hf_target",
    "repay_amount": "AssessmentResponse.repay_amount",
    "collateral_asset": "AssessmentResponse.collateral_asset",
    "est_cost_bps": "AssessmentResponse.est_cost_bps",
    "viable": "AssessmentResponse.viable",
    "reason": "AssessmentResponse.reason",
    "sigma": "RiskSignal.sigma",
    "breach_probability": "RiskSignal.breach_probability",
    "hf_target_bps": "RiskSignal.hf_target_bps",
    "hf_trigger_bps": "PositionSnapshot.hf_trigger_bps",
    "state": "PositionSnapshot.state",
    "collateral_usd": "UserAccountData.total_collateral_base",
    "debt_usd": "UserAccountData.total_debt_base",
    "liquidation_threshold_bps": "UserAccountData.liquidation_threshold_bps",
    "hf_bps": "UserAccountData.health_factor",
}


class FactSheet(BaseModel):
    """Every figure the agent is allowed to state, with its provenance.

    Deliberately flat and JSON-serialisable: it is stored on the proposal row, returned on the
    wire, and rendered directly. Nothing here is derived by a model.
    """

    borrower: str
    as_of: str

    # Health
    hf: float
    hf_bps: int
    hf_target: float
    hf_target_bps: int
    hf_trigger_bps: int
    state: str

    # Position
    collateral_usd: float
    debt_usd: float
    liquidation_threshold_bps: int

    # Proposed intervention
    repay_amount: int
    repay_amount_human: str
    collateral_asset: str
    collateral_symbol: str
    est_cost_bps: int
    viable: bool
    reason: str | None = None

    # Risk signal — computed on every assessment, and until now thrown away.
    sigma: float = 0.0
    breach_probability: float = 0.0

    sources: dict[str, str] = Field(default_factory=dict)

    def numeric_leaves(self) -> list[float]:
        """Every number this sheet asserts, for :mod:`app.agent.guard` to check prose against."""
        return [
            self.hf, self.hf_bps, self.hf_target, self.hf_target_bps, self.hf_trigger_bps,
            self.collateral_usd, self.debt_usd, self.liquidation_threshold_bps,
            self.repay_amount, self.est_cost_bps, self.sigma, self.breach_probability,
        ]


def _symbol_for(asset: str) -> str:
    """Human ticker for an address, falling back to a truncated address when unknown."""
    return TOKEN_SYMBOLS.get(asset) or f"{asset[:6]}…{asset[-4:]}"


def _human_amount(amount: int, asset: str) -> str:
    """Render a raw token amount at its own decimals. Unknown assets stay raw rather than guess."""
    decimals = TOKEN_DECIMALS.get(asset)
    if decimals is None:
        return str(amount)
    return f"{amount / 10**decimals:,.2f}"


def build_factsheet(
    *,
    snapshot: PositionSnapshot,
    assessment: AssessmentResponse,
    risk: RiskSignal | None = None,
    debt_asset: str | None = None,
    now: datetime | None = None,
) -> FactSheet:
    """Assemble the sheet from backend responses. Reads fields; computes nothing new.

    The only arithmetic here is unit conversion (WAD→bps, base-currency→USD, raw→decimal), which
    reproduces conventions already established in ``app.core.monitor`` and ``UserAccountData``.
    """
    account = snapshot.account
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")

    return FactSheet(
        borrower=snapshot.borrower,
        as_of=stamp,
        hf=assessment.hf,
        hf_bps=account.health_factor // (10**14),  # WAD -> bps, as classify_state does
        hf_target=assessment.hf_target,
        hf_target_bps=risk.hf_target_bps if risk else round(assessment.hf_target * BPS),
        hf_trigger_bps=snapshot.hf_trigger_bps,
        state=snapshot.state.value,
        collateral_usd=account.total_collateral_base / 10**AAVE_BASE_CURRENCY_DECIMALS,
        debt_usd=account.total_debt_base / 10**AAVE_BASE_CURRENCY_DECIMALS,
        liquidation_threshold_bps=account.liquidation_threshold_bps,
        repay_amount=assessment.repay_amount,
        repay_amount_human=_human_amount(assessment.repay_amount, debt_asset or ""),
        collateral_asset=assessment.collateral_asset,
        collateral_symbol=_symbol_for(assessment.collateral_asset),
        est_cost_bps=assessment.est_cost_bps,
        viable=assessment.viable,
        reason=assessment.reason,
        sigma=risk.sigma if risk else 0.0,
        breach_probability=risk.breach_probability if risk else 0.0,
        sources=dict(_SOURCES),
    )
