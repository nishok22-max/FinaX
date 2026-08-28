"""Unit tests for the circuit breaker (FR-17)."""
from __future__ import annotations

from app.core.breaker import CircuitBreaker, TripReason


def test_trips_after_n_consecutive_failures() -> None:
    b = CircuitBreaker(max_consecutive_failures=3)
    assert b.allow()
    b.record_failure()
    b.record_failure()
    assert b.allow()          # 2 failures, still armed
    b.record_failure()
    assert not b.allow()      # 3rd trips it
    assert b.paused
    assert b.trip_reason == TripReason.CONSECUTIVE_FAILURES.value


def test_success_resets_streak() -> None:
    b = CircuitBreaker(max_consecutive_failures=3)
    b.record_failure()
    b.record_failure()
    b.record_success()        # streak cleared
    b.record_failure()
    b.record_failure()
    assert b.allow()          # only 2 since reset
    assert b.consecutive_failures == 2


def test_hard_trip_signal() -> None:
    b = CircuitBreaker()
    b.trip(TripReason.STALE_ORACLE)
    assert b.paused
    assert not b.allow()
    assert b.trip_reason == "stale_oracle"


def test_operator_reset() -> None:
    b = CircuitBreaker(max_consecutive_failures=1)
    b.record_failure()
    assert b.paused
    b.reset()
    assert b.allow()
    assert b.consecutive_failures == 0
    assert b.trip_reason is None


def test_live_threshold_change() -> None:
    b = CircuitBreaker(max_consecutive_failures=3)
    b.max_consecutive_failures = 2
    b.record_failure()
    b.record_failure()
    assert b.paused
