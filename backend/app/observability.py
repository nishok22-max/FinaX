"""Structured logging + decision counters (NFR-5).

A tiny in-process metrics registry the service increments at every decision point (assessed,
declined, simulated, submitted, restored, reverted, breaker trips, double-submit blocks) and
surfaces through ``GET /metrics``. Kept dependency-free (no Prometheus client) so the hackathon
build stays self-contained; the counter names are stable for later export.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from threading import Lock


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent structured-ish logging setup (safe to call from app startup)."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)


class Counters:
    """Thread-safe monotonic counters keyed by name."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = Lock()

    def inc(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counts[name] += by

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts[name]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


# Stable counter names.
ASSESSED = "assessed"
DECLINED = "declined"
SIMULATED_OK = "simulated_ok"
SIMULATED_FAIL = "simulated_fail"
SUBMITTED = "submitted"
RESTORED = "restored"
REVERTED = "reverted"
DOUBLE_SUBMIT_BLOCKED = "double_submit_blocked"
BREAKER_TRIPPED = "breaker_tripped"
BREAKER_BLOCKED = "breaker_blocked"
ERRORS = "errors"
