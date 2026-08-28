"""Pure-unit tests for the Phase 3 data models — no chain, always run."""
from __future__ import annotations

import math

from app.core.models import (
    AssessmentResponse,
    PositionState,
    ReserveInfo,
    RiskParams,
    UserAccountData,
)

BORROWER = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"  # any valid checksum address
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


def _params() -> RiskParams:
    return RiskParams(
        borrower=BORROWER.lower(),
        hf_trigger_bps=11500,
        hf_target_base_bps=12500,
        vol_coeff_k=150,
        hf_target_max_bps=14000,
        max_slippage_bps=50,
        max_cost_bps=200,
        allowed_collaterals=[WETH.lower()],
        nonce=1,
        deadline=1_900_000_000,
    )


def test_risk_params_checksums_addresses() -> None:
    p = _params()
    assert p.borrower == BORROWER  # normalised to checksum
    assert p.allowed_collaterals == [WETH]


def test_risk_params_tuple_order_matches_struct() -> None:
    """Positional tuple must follow the Solidity RiskParams member order exactly."""
    p = _params()
    t = p.to_solidity_tuple()
    assert t == (
        BORROWER,
        11500,
        12500,
        150,
        14000,
        50,
        200,
        [WETH],
        1,
        1_900_000_000,
    )


def test_risk_params_accepts_alias_and_snake_case() -> None:
    """populate_by_name lets both camelCase (JSON) and snake_case construct the model."""
    via_alias = RiskParams.model_validate(
        {
            "borrower": BORROWER,
            "hfTriggerBps": 11500,
            "hfTargetBaseBps": 12500,
            "volCoeffK": 150,
            "hfTargetMaxBps": 14000,
            "maxSlippageBps": 50,
            "maxCostBps": 200,
            "allowedCollaterals": [WETH],
            "nonce": 1,
            "deadline": 1_900_000_000,
        }
    )
    assert via_alias.to_solidity_tuple() == _params().to_solidity_tuple()


def test_eip712_message_uses_camelcase_keys() -> None:
    msg = _params().eip712_message()
    assert set(msg) == {
        "borrower", "hfTriggerBps", "hfTargetBaseBps", "volCoeffK", "hfTargetMaxBps",
        "maxSlippageBps", "maxCostBps", "allowedCollaterals", "nonce", "deadline",
    }


def test_user_account_data_hf_and_usd() -> None:
    uad = UserAccountData(
        total_collateral_base=2_000 * 10**8,  # $2000, 8-decimal base
        total_debt_base=1_500 * 10**8,
        available_borrows_base=0,
        liquidation_threshold_bps=8500,
        ltv_bps=8000,
        health_factor=12 * 10**17,  # 1.2 in WAD
    )
    assert uad.has_debt
    assert math.isclose(uad.hf, 1.2, rel_tol=1e-9)
    assert math.isclose(uad.collateral_usd, 2000.0)
    assert math.isclose(uad.debt_usd, 1500.0)


def test_user_account_data_no_debt_is_infinite_hf() -> None:
    uad = UserAccountData(
        total_collateral_base=1000 * 10**8,
        total_debt_base=0,
        available_borrows_base=0,
        liquidation_threshold_bps=8500,
        ltv_bps=8000,
        health_factor=2**256 - 1,
    )
    assert not uad.has_debt
    assert uad.hf == float("inf")


def test_reserve_info_liq_threshold_fraction() -> None:
    r = ReserveInfo(
        asset=WETH,
        aToken_address=WETH,
        variable_debt_token_address=USDC,
        decimals=18,
        liq_threshold_bps=8500,
    )
    assert math.isclose(r.liq_threshold, 0.85)


def test_assessment_response_roundtrip() -> None:
    a = AssessmentResponse(
        hf=1.18,
        hf_target=1.25,
        repay_amount=500 * 10**6,
        collateral_asset=WETH,
        est_cost_bps=42,
        viable=True,
    )
    assert a.reason is None
    assert AssessmentResponse.model_validate(a.model_dump()) == a


def test_position_state_values() -> None:
    assert PositionState.HEALTHY.value == "HEALTHY"
    assert {s.value for s in PositionState} == {
        "HEALTHY", "WATCH", "ASSESSING", "DECLINED", "READY", "SUBMITTED", "RESTORED", "REVERTED",
    }
