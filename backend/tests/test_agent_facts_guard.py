"""FactSheet construction and NumberGuard provenance checks (FR-22).

Two properties under test:

* the sheet reads its figures off real backend models and reports where each came from, and
* the guard accepts a backed figure in the renderings this product actually uses, while catching
  one the backend never produced.

No LLM, no chain, no store.
"""
from __future__ import annotations

import pytest

from app.agent.facts import build_factsheet
from app.agent.guard import NumberGuard, check, collect_numbers, extract_numbers
from app.core.models import AssessmentResponse, PositionSnapshot, RiskSignal
from app.core.state import PositionState

BORROWER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
UNKNOWN = "0x00000000000000000000000000000000DeaDBeef"


def _snapshot(hf: float = 1.14) -> PositionSnapshot:
    return PositionSnapshot.model_validate({
        "borrower": BORROWER,
        "account": {
            "total_collateral_base": 3_000 * 10**8,
            "total_debt_base": 2_000 * 10**8,
            "available_borrows_base": 0,
            "liquidation_threshold_bps": 8_400,
            "ltv_bps": 8_000,
            "health_factor": int(hf * 10**18),
        },
        "state": PositionState.ASSESSING,
        "hf": hf,
        "hf_trigger_bps": 11_500,
    })


def _assessment() -> AssessmentResponse:
    return AssessmentResponse(
        hf=1.14, hf_target=1.25, repay_amount=400_000_000, collateral_asset=WETH,
        est_cost_bps=80, viable=True, reason=None,
    )


def _risk() -> RiskSignal:
    return RiskSignal(sigma=0.0182, breach_probability=0.0431, hf_target_bps=12_636)


# --- FactSheet -------------------------------------------------------------------------------


def test_factsheet_reads_figures_off_the_backend_models() -> None:
    fs = build_factsheet(
        snapshot=_snapshot(), assessment=_assessment(), risk=_risk(), debt_asset=USDC
    )
    assert fs.borrower == BORROWER
    assert fs.hf == 1.14
    assert fs.hf_target == 1.25
    assert fs.repay_amount == 400_000_000
    assert fs.est_cost_bps == 80
    assert fs.viable is True
    assert fs.state == "ASSESSING"
    assert fs.collateral_usd == 3_000.0
    assert fs.debt_usd == 2_000.0
    assert fs.liquidation_threshold_bps == 8_400


def test_factsheet_recovers_the_risk_signal_the_pipeline_discards() -> None:
    """sigma / breach_probability are computed on every assessment and never surfaced today."""
    fs = build_factsheet(snapshot=_snapshot(), assessment=_assessment(), risk=_risk())
    assert fs.sigma == 0.0182
    assert fs.breach_probability == 0.0431
    assert fs.hf_target_bps == 12_636


def test_factsheet_without_a_risk_signal_falls_back_to_the_assessment_target() -> None:
    fs = build_factsheet(snapshot=_snapshot(), assessment=_assessment(), risk=None)
    assert fs.sigma == 0.0
    assert fs.breach_probability == 0.0
    assert fs.hf_target_bps == 12_500  # round(1.25 * 10_000)


def test_factsheet_converts_hf_wad_to_bps_like_classify_state() -> None:
    fs = build_factsheet(snapshot=_snapshot(hf=1.1432), assessment=_assessment())
    assert fs.hf_bps == 11_432


def test_factsheet_resolves_symbols_and_human_amounts() -> None:
    fs = build_factsheet(snapshot=_snapshot(), assessment=_assessment(), debt_asset=USDC)
    assert fs.collateral_symbol == "WETH"
    assert fs.repay_amount_human == "400.00"  # 400_000_000 at USDC's 6 decimals


def test_factsheet_does_not_guess_decimals_for_an_unknown_asset() -> None:
    """Rendering an unknown token at an assumed scale would invent a figure."""
    fs = build_factsheet(snapshot=_snapshot(), assessment=_assessment(), debt_asset=UNKNOWN)
    assert fs.repay_amount_human == "400000000"


def test_factsheet_carries_a_source_for_every_reported_figure() -> None:
    fs = build_factsheet(snapshot=_snapshot(), assessment=_assessment(), risk=_risk())
    for field in ("hf", "hf_target", "repay_amount", "est_cost_bps", "sigma",
                  "breach_probability", "collateral_usd", "debt_usd"):
        assert field in fs.sources
    assert fs.sources["sigma"] == "RiskSignal.sigma"


def test_numeric_leaves_expose_every_asserted_figure() -> None:
    fs = build_factsheet(snapshot=_snapshot(), assessment=_assessment(), risk=_risk())
    leaves = fs.numeric_leaves()
    assert 1.14 in leaves
    assert 400_000_000 in leaves
    assert 0.0182 in leaves


# --- extract_numbers / collect_numbers -------------------------------------------------------


def test_extract_numbers_reads_separators_and_decimals() -> None:
    assert extract_numbers("HF 1.14, debt 2,000.50 USD, cost 80 bps") == [1.14, 2000.50, 80.0]


def test_extract_numbers_ignores_addresses_and_tx_hashes() -> None:
    text = f"Borrower {BORROWER} has HF 1.14 (tx 0xdeadbeef1234)"
    assert extract_numbers(text) == [1.14]


def test_extract_numbers_ignores_iso_timestamps() -> None:
    assert extract_numbers("As of 2026-08-28T13:45:00+00:00 the HF is 1.14") == [1.14]


def test_collect_numbers_walks_nested_json_and_skips_booleans() -> None:
    found = collect_numbers({"hf": 1.14, "viable": True, "inner": {"bps": [80, 500]}})
    assert sorted(found) == [1.14, 80.0, 500.0]


# --- check -----------------------------------------------------------------------------------


def test_prose_using_only_backed_figures_passes() -> None:
    ok, bad = check("HF is 1.14 against a target of 1.25.", [1.14, 1.25])
    assert ok and bad == []


@pytest.mark.parametrize(
    "text,allowed",
    [
        ("cost is 0.80%", [80]),               # bps rendered as a percent
        ("breach probability 4.31%", [0.0431]),  # ratio rendered as a percent
        ("HF 1.14", [1.1432]),                  # rounded to 2dp
        ("repay 400.00 USDC", [400_000_000]),   # 6dp token units in whole units
        ("target 1.2636", [12_636]),            # bps rendered as a ratio
    ],
)
def test_legitimate_renderings_of_a_backed_figure_pass(text: str, allowed: list[float]) -> None:
    ok, bad = check(text, allowed)
    assert ok, f"wrongly flagged {bad}"


def test_an_invented_figure_is_caught() -> None:
    ok, bad = check("HF is 1.14 and will reach 1.32 after the rescue.", [1.14, 1.25])
    assert not ok
    assert bad == [1.32]


def test_bare_ordinals_and_denominators_are_not_flagged() -> None:
    ok, _ = check("There are 2 steps and 3 checks; bps are out of 10000.", [])
    assert ok


def test_tolerance_admits_a_last_digit_rounding_difference() -> None:
    ok, _ = check("cost 80.1 bps", [80.0])
    assert ok


# --- NumberGuard -----------------------------------------------------------------------------


def test_guard_allows_what_the_tools_actually_returned() -> None:
    guard = NumberGuard()
    guard.observe({"hf": 1.14, "hf_target": 1.25, "est_cost_bps": 80})
    ok, bad = guard.verify("HF 1.14 against target 1.25 at a cost of 80 bps.")
    assert ok and bad == []


def test_guard_flags_a_figure_no_tool_returned() -> None:
    guard = NumberGuard()
    guard.observe({"hf": 1.14})
    ok, bad = guard.verify("HF 1.14, and the liquidation penalty would be 7.5%.")
    assert not ok
    assert bad == [7.5]


def test_guard_accepts_the_whole_factsheet_as_provenance() -> None:
    guard = NumberGuard()
    fs = build_factsheet(snapshot=_snapshot(), assessment=_assessment(), risk=_risk(),
                         debt_asset=USDC)
    guard.observe(fs.model_dump())
    ok, bad = guard.verify(
        "Health factor 1.14 sits below the 11500 bps trigger; repaying 400.00 USDC "
        "lifts it to 1.25 at an estimated 80 bps."
    )
    assert ok, f"wrongly flagged {bad}"


def test_annotate_appends_a_visible_banner_and_reports_the_flag() -> None:
    guard = NumberGuard()
    guard.observe({"hf": 1.14})
    text, flagged, bad = guard.annotate("HF 1.14 will become 1.32.")
    assert flagged is True
    assert bad == [1.32]
    assert "Unverified figures" in text
    assert "1.32" in text
    # The original prose is preserved — flagging marks a reply, it does not suppress it.
    assert text.startswith("HF 1.14 will become 1.32.")


def test_annotate_leaves_a_clean_reply_untouched() -> None:
    guard = NumberGuard()
    guard.observe({"hf": 1.14})
    text, flagged, bad = guard.annotate("HF is 1.14.")
    assert (text, flagged, bad) == ("HF is 1.14.", False, [])
