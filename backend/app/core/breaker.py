"""Circuit breaker (FR-17) — pause autonomous submission when things go wrong.

Trips after N consecutive failures (default 3) or on a hard safety signal (stale oracle,
inconsistent RPC, invalid quote, gas spike). Once tripped it stays paused until an operator
resets it, so a misbehaving keeper cannot keep firing. State is exposed via ``/metrics``.
"""
from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class TripReason(str, Enum):
    CONSECUTIVE_FAILURES = "consecutive_failures"
    STALE_ORACLE = "stale_oracle"
    RPC_INCONSISTENT = "rpc_inconsistent"
    INVALID_QUOTE = "invalid_quote"
    GAS_SPIKE = "gas_spike"
    MANUAL = "manual"


class CircuitBreaker:
    """Consecutive-failure + hard-signal breaker guarding autonomous submission."""

    def __init__(self, max_consecutive_failures: int = 3) -> None:
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        self._max = max_consecutive_failures
        self._consecutive = 0
        self._paused = False
        self._reason: TripReason | None = None

    @property
    def max_consecutive_failures(self) -> int:
        return self._max

    @max_consecutive_failures.setter
    def max_consecutive_failures(self, value: int) -> None:
        if value < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        self._max = value

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive

    @property
    def trip_reason(self) -> str | None:
        return self._reason.value if self._reason else None

    def allow(self) -> bool:
        """True if autonomous submission is currently permitted."""
        return not self._paused

    def record_success(self) -> None:
        """A resolved successful rescue clears the failure streak (but not a manual pause)."""
        self._consecutive = 0

    def record_failure(self) -> None:
        """Count a failed rescue; trip once the streak reaches the threshold."""
        self._consecutive += 1
        if self._consecutive >= self._max and not self._paused:
            self._trip(TripReason.CONSECUTIVE_FAILURES)

    def trip(self, reason: TripReason) -> None:
        """Force an immediate pause on a hard safety signal."""
        self._trip(reason)

    def _trip(self, reason: TripReason) -> None:
        self._paused = True
        self._reason = reason
        logger.error("circuit breaker TRIPPED: reason=%s streak=%d", reason.value, self._consecutive)

    def reset(self) -> None:
        """Operator reset: clear the pause and the failure streak."""
        logger.warning("circuit breaker reset by operator (was reason=%s)", self.trip_reason)
        self._paused = False
        self._reason = None
        self._consecutive = 0
