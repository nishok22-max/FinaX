"""Unit tests for the in-flight lock + cooldown (FR-16)."""
from __future__ import annotations

from app.core.inflight import InFlightRegistry

B = "0x0000000000000000000000000000000000000001"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_acquire_blocks_second_start() -> None:
    reg = InFlightRegistry(cooldown_seconds=30, clock=FakeClock())
    assert reg.acquire(B)          # first acquires
    assert not reg.acquire(B)      # second blocked while in flight
    assert reg.is_locked(B)


def test_release_starts_cooldown() -> None:
    clock = FakeClock()
    reg = InFlightRegistry(cooldown_seconds=30, clock=clock)
    reg.acquire(B)
    reg.release(B)
    assert not reg.is_locked(B)
    assert reg.in_cooldown(B)
    assert not reg.can_start(B)     # cooling down
    clock.advance(31)
    assert not reg.in_cooldown(B)
    assert reg.can_start(B)         # re-armed after cooldown


def test_can_start_when_idle() -> None:
    reg = InFlightRegistry(cooldown_seconds=30, clock=FakeClock())
    assert reg.can_start(B)


def test_locked_borrowers_listing() -> None:
    reg = InFlightRegistry(cooldown_seconds=0, clock=FakeClock())
    reg.acquire(B)
    assert reg.locked_borrowers() == [B]
    reg.release(B)
    assert reg.locked_borrowers() == []
