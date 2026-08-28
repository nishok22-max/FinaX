"""Position monitoring (FR-1) — poll HF + prices and classify the lifecycle state.

Phase 4 delivers the polling primitive and the pure state classifier; Phase 5 wires these into
the APScheduler worker loop and the full ``PositionState`` transition graph. ``classify_state`` is a
pure function so it is trivially unit-testable; ``PositionMonitor.poll_once`` reads a live snapshot
and feeds the collateral price into a :class:`RollingVolatility` for the risk model.
"""
from __future__ import annotations

from app.chain.aave import AaveClient
from app.chain.oracle import OracleClient
from app.config.arbitrum import BPS
from app.core.models import PositionSnapshot, PositionState, UserAccountData
from app.core.risk import RollingVolatility


def classify_state(
    account: UserAccountData,
    hf_trigger_bps: int,
    *,
    watch_band_bps: int = 500,
) -> PositionState:
    """Map an account snapshot to a coarse lifecycle state (Phase 4 subset).

    * No debt → ``HEALTHY``.
    * ``HF <= trigger`` → ``ASSESSING`` (intervention warranted; FR-12 proactive action).
    * ``HF <= trigger + watch_band`` → ``WATCH`` (approaching the trigger).
    * otherwise → ``HEALTHY``.
    """
    if not account.has_debt:
        return PositionState.HEALTHY
    hf_bps = account.health_factor // (10**14)  # WAD -> bps
    if hf_bps <= hf_trigger_bps:
        return PositionState.ASSESSING
    if hf_bps <= hf_trigger_bps + watch_band_bps:
        return PositionState.WATCH
    return PositionState.HEALTHY


class PositionMonitor:
    """Reads a borrower's Aave snapshot and tracks collateral-price volatility."""

    def __init__(
        self,
        aave: AaveClient,
        oracle: OracleClient,
        *,
        vol_window: int = 30,
    ) -> None:
        self._aave = aave
        self._oracle = oracle
        self._vols: dict[str, RollingVolatility] = {}
        self._vol_window = vol_window

    def volatility_for(self, asset: str) -> RollingVolatility:
        vol = self._vols.get(asset)
        if vol is None:
            vol = RollingVolatility(window=self._vol_window)
            self._vols[asset] = vol
        return vol

    async def poll_once(self, borrower: str, hf_trigger_bps: int) -> PositionSnapshot:
        """One monitor tick: read account data and classify state (no submission)."""
        account = await self._aave.get_user_account_data(borrower)
        state = classify_state(account, hf_trigger_bps)
        return PositionSnapshot(
            borrower=borrower,
            account=account,
            state=state,
            hf=account.hf,
            hf_trigger_bps=hf_trigger_bps,
        )

    async def sample_price(self, asset: str) -> float:
        """Read the current oracle price for ``asset`` and record it for volatility tracking."""
        price = await self._oracle.get_asset_price(asset)
        value = price.price / (10**price.decimals)
        self.volatility_for(asset).update(value)
        return value

    def sigma_for(self, asset: str) -> float:
        return self.volatility_for(asset).sigma()


# Exposed for callers converting HF WAD<->bps in the same convention as classify_state.
def hf_wad_to_bps(hf_wad: int) -> int:
    return hf_wad // (10**14)


def hf_bps_to_float(hf_bps: int) -> float:
    return hf_bps / BPS
