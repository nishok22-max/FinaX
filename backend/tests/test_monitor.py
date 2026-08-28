"""Unit tests for monitor state classification (FR-1)."""
from __future__ import annotations

from app.core.models import PositionState, UserAccountData
from app.core.monitor import classify_state


def _account(hf: float, debt_base: int = 1_000 * 10**8) -> UserAccountData:
    return UserAccountData(
        total_collateral_base=2_000 * 10**8,
        total_debt_base=debt_base,
        available_borrows_base=0,
        liquidation_threshold_bps=8_500,
        ltv_bps=8_000,
        health_factor=int(hf * 10**18) if debt_base else 2**256 - 1,
    )


def test_no_debt_is_healthy() -> None:
    assert classify_state(_account(0, debt_base=0), 11_500) == PositionState.HEALTHY


def test_below_trigger_is_assessing() -> None:
    # HF 1.10 vs trigger 1.15 -> action warranted.
    assert classify_state(_account(1.10), 11_500) == PositionState.ASSESSING


def test_just_above_trigger_is_watch() -> None:
    # HF 1.18 within the 500-bps watch band above trigger 1.15.
    assert classify_state(_account(1.18), 11_500) == PositionState.WATCH


def test_well_above_trigger_is_healthy() -> None:
    assert classify_state(_account(1.80), 11_500) == PositionState.HEALTHY


def test_trigger_boundary_is_assessing() -> None:
    # Exactly at the trigger counts as actionable.
    assert classify_state(_account(1.15), 11_500) == PositionState.ASSESSING
