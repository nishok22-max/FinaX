"""Explicit position state machine (FR-16) — the single source of truth for `PositionState`.

Making the lifecycle explicit is what gives the keeper idempotency and no-duplicate-rescue
guarantees: a borrower in ``SUBMITTED`` holds the in-flight lock and cannot be re-submitted until
the receipt resolves. ``VALID_TRANSITIONS`` mirrors the state diagram in architecture §7; the
worker uses ``can_transition`` to reject illegal jumps rather than silently corrupting state.
"""
from __future__ import annotations

from enum import Enum


class PositionState(str, Enum):
    """Lifecycle of a monitored position (architecture §7)."""

    HEALTHY = "HEALTHY"      # HF comfortable
    WATCH = "WATCH"          # HF <= HF_trigger (buffer), reassessing
    ASSESSING = "ASSESSING"  # sizing + selection + viability + simulation
    DECLINED = "DECLINED"    # not viable / no liquidity (transient)
    READY = "READY"          # simulated OK, about to submit
    SUBMITTED = "SUBMITTED"  # tx broadcast, in-flight lock held, awaiting receipt
    RESTORED = "RESTORED"    # HF_after >= HF_target
    REVERTED = "REVERTED"    # atomic revert, position unchanged


# Allowed transitions (architecture §7 state diagram). Terminal outcomes route back to the
# monitoring states once the in-flight lock clears.
VALID_TRANSITIONS: dict[PositionState, frozenset[PositionState]] = {
    PositionState.HEALTHY: frozenset({PositionState.WATCH}),
    PositionState.WATCH: frozenset({PositionState.HEALTHY, PositionState.ASSESSING}),
    PositionState.ASSESSING: frozenset(
        {PositionState.DECLINED, PositionState.WATCH, PositionState.READY}
    ),
    PositionState.DECLINED: frozenset({PositionState.WATCH}),
    PositionState.READY: frozenset({PositionState.SUBMITTED, PositionState.WATCH}),
    PositionState.SUBMITTED: frozenset({PositionState.RESTORED, PositionState.REVERTED}),
    PositionState.RESTORED: frozenset({PositionState.HEALTHY}),
    PositionState.REVERTED: frozenset({PositionState.WATCH}),
}

# States in which a rescue is in progress and no new one may start (FR-16).
IN_FLIGHT_STATES: frozenset[PositionState] = frozenset(
    {PositionState.READY, PositionState.SUBMITTED}
)


def can_transition(src: PositionState, dst: PositionState) -> bool:
    """True if ``src -> dst`` is a legal edge (a self-loop is always allowed)."""
    if src is dst:
        return True
    return dst in VALID_TRANSITIONS.get(src, frozenset())


def is_terminal_outcome(state: PositionState) -> bool:
    """RESTORED / REVERTED are the receipt-resolved outcomes that release the in-flight lock."""
    return state in (PositionState.RESTORED, PositionState.REVERTED)
