"""Unit tests for the position state machine (FR-16)."""
from __future__ import annotations

from app.core.state import (
    IN_FLIGHT_STATES,
    PositionState,
    can_transition,
    is_terminal_outcome,
)


def test_happy_path_transitions_are_legal() -> None:
    path = [
        PositionState.HEALTHY, PositionState.WATCH, PositionState.ASSESSING,
        PositionState.READY, PositionState.SUBMITTED, PositionState.RESTORED,
        PositionState.HEALTHY,
    ]
    from itertools import pairwise

    for src, dst in pairwise(path):
        assert can_transition(src, dst), f"{src}->{dst} should be legal"


def test_revert_path_transitions_are_legal() -> None:
    assert can_transition(PositionState.SUBMITTED, PositionState.REVERTED)
    assert can_transition(PositionState.REVERTED, PositionState.WATCH)


def test_illegal_transitions_rejected() -> None:
    assert not can_transition(PositionState.HEALTHY, PositionState.SUBMITTED)
    assert not can_transition(PositionState.ASSESSING, PositionState.RESTORED)
    assert not can_transition(PositionState.RESTORED, PositionState.SUBMITTED)


def test_self_loop_allowed() -> None:
    assert can_transition(PositionState.WATCH, PositionState.WATCH)


def test_in_flight_states() -> None:
    assert IN_FLIGHT_STATES == frozenset({PositionState.READY, PositionState.SUBMITTED})


def test_terminal_outcomes() -> None:
    assert is_terminal_outcome(PositionState.RESTORED)
    assert is_terminal_outcome(PositionState.REVERTED)
    assert not is_terminal_outcome(PositionState.SUBMITTED)
