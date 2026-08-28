"""Risk model (FR-2, FR-10) — rolling volatility, breach probability, dynamic HF target.

Deliberately *not* ML (NG6): a simple rolling standard deviation of periodic price returns feeds
both a breach-probability estimate and the volatility-scaled ``HF_target``. Everything here is a
pure function of numbers so it is fully unit-testable; the chain clients feed it prices upstream.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable

from app.config.arbitrum import BPS
from app.core.models import RiskSignal


class RollingVolatility:
    """Fixed-window realized volatility from a price series (stdev of simple returns).

    Feed the collateral (or collateral/debt) price each poll tick; ``sigma`` returns the sample
    standard deviation of the most recent ``window`` period-over-period returns. Fewer than two
    returns yields ``0.0`` (treated as "no measured volatility").
    """

    def __init__(self, window: int = 30) -> None:
        if window < 2:
            raise ValueError("window must be >= 2")
        self.window = window
        self._prices: deque[float] = deque(maxlen=window + 1)

    def update(self, price: float) -> None:
        if price > 0:
            self._prices.append(float(price))

    def extend(self, prices: Iterable[float]) -> None:
        for p in prices:
            self.update(p)

    def returns(self) -> list[float]:
        p = list(self._prices)
        return [(p[i] / p[i - 1]) - 1.0 for i in range(1, len(p)) if p[i - 1] > 0]

    def sigma(self) -> float:
        r = self.returns()
        if len(r) < 2:
            return 0.0
        mean = sum(r) / len(r)
        var = sum((x - mean) ** 2 for x in r) / (len(r) - 1)  # sample variance
        return math.sqrt(var)


def breach_probability(hf: float, sigma: float, horizon_periods: int = 1) -> float:
    """Rough P(HF crosses 1.0 within ``horizon_periods``) given current ``hf`` and per-period σ.

    Models the log-distance to the boundary in standard deviations and reads a normal tail:
    ``z = ln(hf) / (σ·√horizon)`` → ``P = Φ(−z)``. A heuristic early-warning signal, not a
    calibrated forecast — the on-chain HealthGuard remains the authority. Returns 0 when there is
    no volatility or the position already sits at/below the boundary edge cases are handled.
    """
    if hf <= 0:
        return 1.0
    if hf == float("inf"):
        return 0.0
    if sigma <= 0 or horizon_periods <= 0:
        return 0.0 if hf > 1.0 else 1.0
    z = math.log(hf) / (sigma * math.sqrt(horizon_periods))
    # Φ(−z) via the error function.
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def dynamic_hf_target_bps(sigma: float, base_bps: int, max_bps: int, k: int) -> int:
    """Volatility-scaled ``HF_target`` in bps: ``base + k·σ``, clamped to ``[base, max]`` (FR-10).

    ``base_bps`` already encodes ``1.0 + base_margin`` (e.g. 12500 = 1.25). ``k`` is the borrower's
    signed ``volCoeffK`` — HF-target basis points added per unit of σ. Low σ leaves the target at
    the floor (~1.25); high σ drives it to the signed ceiling (e.g. 1.40).
    """
    if max_bps < base_bps:
        raise ValueError("max_bps must be >= base_bps")
    delta = int(k * sigma)
    if delta < 0:
        delta = 0
    return max(base_bps, min(base_bps + delta, max_bps))


def assess_risk(
    hf: float,
    sigma: float,
    *,
    base_bps: int,
    max_bps: int,
    k: int,
    horizon_periods: int = 1,
) -> RiskSignal:
    """Bundle the risk outputs the pipeline needs into a :class:`RiskSignal`."""
    target = dynamic_hf_target_bps(sigma, base_bps=base_bps, max_bps=max_bps, k=k)
    return RiskSignal(
        sigma=sigma,
        breach_probability=breach_probability(hf, sigma, horizon_periods),
        hf_target_bps=target,
    )


def hf_target_float(sigma: float, base_bps: int, max_bps: int, k: int) -> float:
    """Convenience: dynamic target as a float HF (e.g. 1.25)."""
    return dynamic_hf_target_bps(sigma, base_bps, max_bps, k) / BPS
