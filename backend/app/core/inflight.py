"""Per-borrower in-flight lock + cooldown (FR-16) — idempotency, no duplicate rescues.

``acquire`` sets a lock when a rescue is submitted; while held, a second trigger for that borrower
is refused, so a duplicate signal (or a fast re-poll) can never double-submit. ``release`` clears
the lock when the receipt resolves and starts a short cooldown before the borrower can be re-armed,
absorbing oracle jitter right after a rescue. Time is injectable for deterministic tests.
"""
from __future__ import annotations

import time
from collections.abc import Callable


class InFlightRegistry:
    """Tracks which borrowers have a rescue in flight and their post-rescue cooldowns."""

    def __init__(self, cooldown_seconds: float = 30.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._locked: dict[str, float] = {}       # borrower -> lock acquired time
        self._cooldown_until: dict[str, float] = {}  # borrower -> earliest re-arm time

    @property
    def cooldown_seconds(self) -> float:
        return self._cooldown

    @cooldown_seconds.setter
    def cooldown_seconds(self, value: float) -> None:
        self._cooldown = max(0.0, value)

    def is_locked(self, borrower: str) -> bool:
        return borrower in self._locked

    def in_cooldown(self, borrower: str) -> bool:
        until = self._cooldown_until.get(borrower)
        return until is not None and self._clock() < until

    def can_start(self, borrower: str) -> bool:
        """True only if the borrower is neither in flight nor cooling down."""
        return not self.is_locked(borrower) and not self.in_cooldown(borrower)

    def acquire(self, borrower: str) -> bool:
        """Take the in-flight lock; returns False if a rescue is already in progress/cooling down."""
        if not self.can_start(borrower):
            return False
        self._locked[borrower] = self._clock()
        return True

    def release(self, borrower: str) -> None:
        """Release the lock once the receipt resolves and begin the cooldown window."""
        self._locked.pop(borrower, None)
        self._cooldown_until[borrower] = self._clock() + self._cooldown

    def locked_borrowers(self) -> list[str]:
        return list(self._locked)
