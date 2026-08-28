"""Tool-layer tests — the agent's entire capability surface, exercised without an LLM.

Two things this file is really asserting:

* the read tools return the *backend's* numbers, unchanged, and
* the capability surface contains nothing that can submit a transaction, and nothing that leaks
  the borrower's signature.

Uses the same fake-service construction as ``test_protection_service.py`` so the tools are
exercised against the real :class:`ProtectionService`, not a mock of it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app import observability as obs
from app.agent import tools
from app.agent.models import GateCheck, PolicyDecision, TuningStatus
from app.agent.store import AgentStore
from app.core.breaker import CircuitBreaker
from app.core.inflight import InFlightRegistry
from app.core.protection_service import ProtectionService
from app.core.state import PositionState
from tests.test_protection_service import (
    BORROWER,
    USDC,
    WETH,
    FakePipeline,
    FakeSimulator,
    FakeSubmitter,
    _assessment,
    _params,
    _plan,
)


class _Monitor:
    """Monitor stub: a fixed snapshot plus a σ window the agent can read back."""

    def __init__(self, hf: float = 1.14, sigma: float = 0.0182) -> None:
        self._hf = hf
        self._sigma = sigma

    def sigma_for(self, asset: str) -> float:
        return self._sigma

    async def sample_price(self, asset: str) -> float:
        return 1.0

    async def poll_once(self, borrower: str, hf_trigger_bps: int = 0):  # type: ignore[no-untyped-def]
        from app.core.models import PositionSnapshot, UserAccountData
        from app.core.monitor import classify_state

        uad = UserAccountData(
            total_collateral_base=3_000 * 10**8,
            total_debt_base=2_000 * 10**8,
            available_borrows_base=0,
            liquidation_threshold_bps=8_400,
            ltv_bps=8_000,
            health_factor=int(self._hf * 10**18),
        )
        trigger = hf_trigger_bps or 11_500
        return PositionSnapshot(
            borrower=borrower, account=uad, state=classify_state(uad, trigger),
            hf=uad.hf, hf_trigger_bps=trigger,
        )


def _service(*, viable: bool = True, hf: float = 1.14) -> ProtectionService:
    return ProtectionService(
        FakePipeline(_assessment(viable=viable), _plan() if viable else None),
        _Monitor(hf=hf),  # type: ignore[arg-type]
        inflight=InFlightRegistry(cooldown_seconds=0),
        breaker=CircuitBreaker(3),
        counters=obs.Counters(),
        simulator=FakeSimulator(True),
        submitter=FakeSubmitter(1),
    )


@pytest.fixture
def service() -> ProtectionService:
    svc = _service()
    svc.register(_params(), "0x00")
    return svc


@pytest.fixture
async def store():  # type: ignore[no-untyped-def]
    s = AgentStore(":memory:")
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


def _gate(allowed: bool = True) -> PolicyDecision:
    return PolicyDecision(
        allowed=allowed,
        checks=[GateCheck(name="registered", passed=allowed, detail="d")],
        blocking=[] if allowed else ["breaker_ok"],
        severity="ok" if allowed else "hard_block",
    )


# --- The capability surface itself -------------------------------------------------------------


def test_no_tool_can_submit_a_transaction() -> None:
    """The central safety property: execution is not in the agent's vocabulary at all."""
    exported = {n for n in dir(tools) if n.startswith("t_")}
    forbidden = {"t_protect", "t_submit", "t_execute", "t_execute_protection", "t_send_tx",
                 "t_sign", "t_repay", "t_withdraw", "t_swap"}
    assert exported & forbidden == set()
    # And nothing in the module reaches the submitter or the protect entrypoint.
    assert tools.__file__
    text = Path(tools.__file__).read_text(encoding="utf-8")
    for banned in ("service.protect(", "Submitter(", "_submitter", "encode_repay",
                   "encode_withdraw", "send_raw_transaction"):
        assert banned not in text, f"tools.py references {banned!r}"


def test_only_the_two_proposal_tools_write() -> None:
    write_tools = {n for n in dir(tools) if n.startswith("t_propose")}
    assert write_tools == {"t_propose_protection", "t_propose_tuning"}


# --- Read-only tools ---------------------------------------------------------------------------


async def test_doctrine_states_where_the_agent_stops() -> None:
    text = await tools.t_doctrine()
    assert "math proposes, simulation validates, Solidity enforces" in text
    assert "not the decision-maker" in text


async def test_list_positions_returns_the_registry(service: ProtectionService) -> None:
    assert await tools.t_list_positions(service) == [BORROWER]


async def test_position_state_reports_state_and_meaning(service: ProtectionService) -> None:
    out = await tools.t_position_state(service, BORROWER)
    assert out["state"] == PositionState.HEALTHY.value
    assert "comfortably above" in out["meaning"]


async def test_position_snapshot_matches_the_backend_numbers(service: ProtectionService) -> None:
    out = await tools.t_position_snapshot(service, BORROWER)
    assert out["borrower"] == BORROWER
    assert out["hf"] == pytest.approx(1.14, abs=1e-9)
    assert out["collateral_usd"] == 3_000.0
    assert out["debt_usd"] == 2_000.0
    assert out["has_debt"] is True
    assert out["registered"] is True
    assert out["hf_trigger_bps"] == _params().hf_trigger_bps


async def test_position_snapshot_for_an_unregistered_borrower_uses_the_rest_default() -> None:
    """Agrees with routes_positions.py, so the two views of a position cannot disagree."""
    out = await tools.t_position_snapshot(_service(), BORROWER)
    assert out["registered"] is False
    assert out["hf_trigger_bps"] == 11_500


async def test_registered_params_never_returns_the_signature() -> None:
    """The signature is the borrower's authorisation and is useless for explanation.

    Withheld rather than merely unused: a tool result is text the model may echo verbatim.
    """
    secret = "0xDEADBEEFCAFE"
    svc = _service()
    svc.register(_params(), secret)

    out = await tools.t_registered_params(svc, BORROWER)
    assert out is not None
    assert secret not in str(out)
    assert set(out) == {
        "borrower", "hf_trigger_bps", "hf_target_base_bps", "hf_target_max_bps",
        "vol_coeff_k", "max_slippage_bps", "max_cost_bps", "allowed_collaterals",
        "allowed_collateral_symbols", "nonce", "deadline",
    }
    assert out["hf_trigger_bps"] == 11_500
    assert out["allowed_collateral_symbols"] == ["WETH"]


async def test_registered_params_is_none_for_an_unknown_borrower() -> None:
    assert await tools.t_registered_params(_service(), BORROWER) is None


async def test_assess_returns_the_pipeline_verdict(service: ProtectionService) -> None:
    out = await tools.t_assess(service, BORROWER)
    expected = _assessment()
    assert out["hf"] == expected.hf
    assert out["hf_target"] == expected.hf_target
    assert out["repay_amount"] == expected.repay_amount
    assert out["viable"] is True
    assert out["collateral_symbol"] == "WETH"


async def test_assess_refuses_an_unregistered_borrower_without_raising() -> None:
    out = await tools.t_assess(_service(), BORROWER)
    assert "not registered" in out["error"]


async def test_risk_signal_recovers_sigma_and_breach_probability(
    service: ProtectionService,
) -> None:
    """These are computed on every assessment and never reach AssessmentResponse."""
    out = await tools.t_risk_signal(service, BORROWER)
    assert out["sigma"] == pytest.approx(0.0182)
    assert 0.0 <= out["breach_probability"] <= 1.0
    p = _params()
    assert p.hf_target_base_bps <= out["hf_target_bps"] <= p.hf_target_max_bps
    assert "not a forecast" in out["note"]


async def test_risk_signal_refuses_an_unregistered_borrower() -> None:
    assert "not registered" in (await tools.t_risk_signal(_service(), BORROWER))["error"]


async def test_metrics_passes_through_the_keeper_snapshot(service: ProtectionService) -> None:
    out = await tools.t_metrics(service)
    assert out["breaker_paused"] is False
    assert out["registered_positions"] == 1
    assert isinstance(out["counters"], dict)


@pytest.mark.parametrize("state", ["ASSESSING", "assessing"])
async def test_explain_state_is_case_insensitive(state: str) -> None:
    out = await tools.t_explain_state(state)
    assert out["state"] == "ASSESSING"
    assert set(out["may_transition_to"]) == {"DECLINED", "WATCH", "READY"}


async def test_explain_state_rejects_an_unknown_state() -> None:
    out = await tools.t_explain_state("SUPERPOSITION")
    assert "unknown state" in out["error"]
    assert "ASSESSING" in out["known_states"]


async def test_explain_revert_covers_every_vault_error() -> None:
    """The vocabulary must match the contract's, or an explanation invents a meaning."""
    vault_errors = {
        "NotAuthorized", "BadSignature", "NonceUsed", "Expired", "CollateralNotAllowed",
        "NoDebt", "TargetOutOfBand", "CallerNotPool", "BadInitiator", "AssetMismatch",
        "CostExceeded", "HealthBelowTarget", "DebtNotReduced", "LeverageIncreased",
    }
    for name in vault_errors:
        out = await tools.t_explain_revert(name)
        assert out["error_name"] == name
        assert len(out["meaning"]) > 20


async def test_explain_revert_tolerates_the_solidity_call_form() -> None:
    assert (await tools.t_explain_revert("HealthBelowTarget()"))["error_name"] == "HealthBelowTarget"


async def test_explain_revert_rejects_an_unknown_error() -> None:
    out = await tools.t_explain_revert("Kaboom")
    assert "not a known vault error" in out["error"]
    assert "BadSignature" in out["known_errors"]


async def test_list_proposals_and_audit_trail_read_the_store(store: AgentStore) -> None:
    from app.agent.models import AuditAction

    pid = await store.insert_proposal(
        run_id="r1", borrower=BORROWER, strategy="protect_now", facts={"hf": 1.14},
        gate=_gate(), rationale="below trigger", ttl_seconds=900,
    )
    await store.audit(actor="agent", action=AuditAction.PROPOSED, borrower=BORROWER,
                      proposal_id=pid, detail={"strategy": "protect_now"})

    proposals = await tools.t_list_proposals(store, borrower=BORROWER)
    assert proposals[0]["id"] == pid
    assert proposals[0]["status"] == "PENDING"
    assert proposals[0]["gate_allowed"] is True

    trail = await tools.t_audit_trail(store, borrower=BORROWER)
    assert trail[0]["action"] == "PROPOSED"


async def test_list_proposals_rejects_an_unknown_status(store: AgentStore) -> None:
    out = await tools.t_list_proposals(store, status="MAYBE")
    assert "unknown status" in out[0]["error"]


# --- Write-gated tools -------------------------------------------------------------------------


async def test_propose_protection_queues_a_pending_row(store: AgentStore) -> None:
    pid = await tools.t_propose_protection(
        store, run_id="r1", borrower=BORROWER, facts={"hf": 1.14},
        rationale="HF below trigger.", gate=_gate(), ttl_seconds=900,
    )
    row = await store.get_proposal(pid)
    assert row is not None
    assert row.status.value == "PENDING"
    assert row.strategy == "protect_now"


async def test_propose_protection_refuses_a_blocked_gate(store: AgentStore) -> None:
    """A refactor must not be able to turn a blocked proposal into an approvable one."""
    with pytest.raises(ValueError, match="policy gate blocked"):
        await tools.t_propose_protection(
            store, run_id="r1", borrower=BORROWER, facts={}, rationale="x",
            gate=_gate(allowed=False), ttl_seconds=900,
        )
    assert await store.list_proposals() == []


async def test_propose_tuning_stores_a_resign_request_not_a_change(store: AgentStore) -> None:
    params = _params()
    tid = await tools.t_propose_tuning(
        store, run_id="r1", borrower=BORROWER, params=params,
        field_name="hf_target_base_bps", suggested_value=13_000,
        rationale="Realized volatility has doubled.",
    )
    row = await store.get_tuning(tid)
    assert row is not None
    assert row.status is TuningStatus.OPEN
    assert row.current_value == params.hf_target_base_bps
    assert row.suggested_value == 13_000
    # The full mandate the borrower must sign, in EIP-712 camelCase.
    assert row.eip712_payload["hfTargetBaseBps"] == 13_000
    assert row.requires_new_signature is True


async def test_propose_tuning_does_not_mutate_the_registered_mandate(
    store: AgentStore, service: ProtectionService
) -> None:
    """FR-21: a mandate altered after signing reverts BadSignature on chain."""
    before = service.params_of(BORROWER)
    assert before is not None
    await tools.t_propose_tuning(
        store, run_id="r1", borrower=BORROWER, params=before[0],
        field_name="max_cost_bps", suggested_value=300, rationale="x",
    )
    after = service.params_of(BORROWER)
    assert after is not None
    assert after[0].max_cost_bps == before[0].max_cost_bps
    assert after[1] == before[1]  # signature untouched


async def test_propose_tuning_rejects_a_mandate_that_would_not_validate(
    store: AgentStore,
) -> None:
    """Suggesting an invalid band would ask the borrower to sign something the API rejects."""
    with pytest.raises(ValidationError, match="hfTargetMaxBps"):
        await tools.t_propose_tuning(
            store, run_id="r1", borrower=BORROWER, params=_params(),
            field_name="hf_target_max_bps", suggested_value=10_000,  # below base (12_500)
            rationale="x",
        )


def test_debt_asset_facts_mirror_the_pipelines_hardcoded_asset() -> None:
    asset, decimals = tools.debt_asset_facts()
    assert asset == USDC
    assert decimals == 6
    assert WETH != asset
