"""Crew-graph tests — topology, routing, the gate, and persistence, all with a fake model.

The point of the graph's design is that everything safety-critical is LLM-free: the gate is a pure
function, the proposal is a row written by Python, and the model contributes one enum and some
prose. These tests exercise that by scripting the enum and asserting on what the *code* did.

Also defines the fake model and runtime helpers reused by ``test_agent_chat.py`` and
``test_agent_api.py``, mirroring how the suite already shares fakes from
``test_protection_service.py``.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app import observability as obs
from app.agent._lazy import agent_stack_available
from app.agent.runtime import AgentRuntime
from app.agent.store import AgentStore
from app.core.breaker import CircuitBreaker, TripReason
from app.core.inflight import InFlightRegistry
from app.core.protection_service import ProtectionService
from tests.test_agent_tools import _Monitor
from tests.test_protection_service import (
    BORROWER,
    FakePipeline,
    FakeSimulator,
    FakeSubmitter,
    _assessment,
    _params,
    _plan,
)

pytestmark = pytest.mark.skipif(
    not agent_stack_available(), reason='the optional agent extra is not installed ([agent])'
)


# --- Fakes -------------------------------------------------------------------------------------


class FakeAIMessage:
    """Minimal stand-in for ``AIMessage`` — content plus optional tool calls."""

    def __init__(self, content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class FakeLLM:
    """Scripted chat model.

    ``structured`` supplies the object returned for a ``with_structured_output`` call; ``script``
    is a queue of replies for plain calls. ``fail`` makes every call raise, which is how the
    outage-degradation paths are tested.
    """

    def __init__(
        self,
        *,
        structured: Any = None,
        script: list[FakeAIMessage] | None = None,
        prose: str = "The position sits just below its trigger.",
        fail: bool = False,
    ) -> None:
        self.structured = structured
        self.script = list(script or [])
        self.prose = prose
        self.fail = fail
        self.calls: list[Any] = []
        self.bound_tools: list[Any] = []

    # -- the LangChain Runnable surface this code actually uses --
    def bind_tools(self, tools: list[Any]) -> FakeLLM:
        self.bound_tools = tools
        return self

    def with_structured_output(self, schema: type) -> _StructuredFake:
        return _StructuredFake(self, schema)

    async def ainvoke(self, payload: Any) -> FakeAIMessage:
        self.calls.append(payload)
        if self.fail:
            raise RuntimeError("model unavailable")
        if self.script:
            return self.script.pop(0)
        return FakeAIMessage(self.prose)


class _StructuredFake:
    def __init__(self, parent: FakeLLM, schema: type) -> None:
        self._parent = parent
        self._schema = schema

    async def ainvoke(self, payload: Any) -> Any:
        self._parent.calls.append(payload)
        if self._parent.fail or self._parent.structured is None:
            raise RuntimeError("model unavailable")
        return self._parent.structured


def make_service(*, viable: bool = True, hf: float = 1.14) -> ProtectionService:
    svc = ProtectionService(
        FakePipeline(_assessment(viable=viable), _plan() if viable else None),
        _Monitor(hf=hf),  # type: ignore[arg-type]
        inflight=InFlightRegistry(cooldown_seconds=0),
        breaker=CircuitBreaker(3),
        counters=obs.Counters(),
        simulator=FakeSimulator(True),
        submitter=FakeSubmitter(1),
    )
    svc.register(_params(), "0x00")
    return svc


@pytest.fixture
async def runtime():  # type: ignore[no-untyped-def]
    store = AgentStore(":memory:")
    await store.connect()
    rt = AgentRuntime(store=store, model="fake-model", api_key="fake-key", timeout_s=5.0)
    try:
        yield rt
    finally:
        await store.close()


@pytest.fixture
def service() -> ProtectionService:
    return make_service()


def _choice(strategy: str, rationale: str = "HF is below its trigger and the pipeline is viable."):  # type: ignore[no-untyped-def]
    from app.agent.graph import StrategyChoice

    return StrategyChoice(strategy=strategy, rationale=rationale)  # type: ignore[arg-type]


async def _run(runtime: AgentRuntime, service: ProtectionService, llm: FakeLLM, **kw: Any):  # type: ignore[no-untyped-def]
    from app.agent.graph import run_crew

    return await run_crew(runtime=runtime, service=service, borrower=BORROWER, llm=llm, **kw)


# --- The happy path ------------------------------------------------------------------------------


async def test_protect_now_passes_the_gate_and_queues_a_proposal(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    result = await _run(runtime, service, FakeLLM(structured=_choice("protect_now")))

    assert result.terminal == "proposed"
    assert result.strategy == "protect_now"
    assert result.proposal_id is not None
    assert result.gate is not None and result.gate.allowed is True

    rows = await runtime.store.list_proposals()
    assert len(rows) == 1
    assert rows[0].status.value == "PENDING"
    assert rows[0].borrower == BORROWER


async def test_the_proposal_carries_backend_facts_not_model_prose(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    result = await _run(runtime, service, FakeLLM(structured=_choice("protect_now")))
    expected = _assessment()
    assert result.facts["hf"] == expected.hf
    assert result.facts["repay_amount"] == expected.repay_amount
    assert result.facts["sources"]["hf"] == "AssessmentResponse.hf"


async def test_a_run_records_a_node_by_node_trace(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    result = await _run(runtime, service, FakeLLM(structured=_choice("protect_now")))
    nodes = [d.node for d in result.trace]
    assert nodes == ["monitor", "analyst", "strategist", "policy_gate", "propose", "auditor"]
    assert [d.llm_used for d in result.trace] == [False, True, True, False, False, True]


async def test_an_audit_row_is_written_for_the_proposal(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    await _run(runtime, service, FakeLLM(structured=_choice("protect_now")))
    actions = [a.action.value for a in await runtime.store.list_audit()]
    assert "PROPOSED" in actions
    assert "CREW_RUN" in actions


# --- The gate is the authority --------------------------------------------------------------------


async def test_a_blocked_gate_queues_nothing(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    """Even when the model insists on acting, a blocked gate means no proposal exists."""
    service.breaker.trip(TripReason.MANUAL)
    result = await _run(runtime, service, FakeLLM(structured=_choice("protect_now")))

    assert result.terminal == "gate_blocked"
    assert result.proposal_id is None
    assert result.gate is not None
    assert "breaker_ok" in result.gate.blocking
    assert result.gate.severity == "hard_block"
    assert await runtime.store.list_proposals() == []


async def test_a_blocked_gate_is_recorded_in_the_audit_trail(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    service.breaker.trip(TripReason.MANUAL)
    await _run(runtime, service, FakeLLM(structured=_choice("protect_now")))
    assert "GATE_BLOCKED" in [a.action.value for a in await runtime.store.list_audit()]


async def test_the_model_cannot_force_action_on_a_non_viable_position(
    runtime: AgentRuntime
) -> None:
    """The pipeline's decline is not overrulable by the strategist."""
    service = make_service(viable=False)
    result = await _run(runtime, service, FakeLLM(structured=_choice("protect_now")))
    assert result.terminal == "gate_blocked"
    assert "assessment_viable" in (result.gate.blocking if result.gate else [])
    assert await runtime.store.list_proposals() == []


async def test_the_model_cannot_force_action_on_a_healthy_position(
    runtime: AgentRuntime
) -> None:
    service = make_service(hf=1.80)
    result = await _run(runtime, service, FakeLLM(structured=_choice("protect_now")))
    assert result.terminal == "gate_blocked"
    assert "hf_below_trigger" in (result.gate.blocking if result.gate else [])


# --- The other routes -----------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["watch", "stand_down"])
async def test_non_acting_strategies_skip_the_gate_entirely(
    runtime: AgentRuntime, service: ProtectionService, strategy: str
) -> None:
    result = await _run(runtime, service, FakeLLM(structured=_choice(strategy)))
    assert result.terminal == "no_action"
    assert result.gate is None
    assert result.proposal_id is None
    assert [d.node for d in result.trace] == ["monitor", "analyst", "strategist", "auditor"]


async def test_retune_writes_a_resign_request_and_no_proposal(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    from app.agent.graph import TuningChoice

    llm = FakeLLM(structured=_choice("retune"))

    # The tuner asks for a TuningChoice; the strategist asked for a StrategyChoice. Swap the
    # scripted object once the strategist has been served.
    original = _StructuredFake.ainvoke

    async def _dispatch(self: _StructuredFake, payload: Any) -> Any:
        if self._schema is TuningChoice:
            return TuningChoice(
                field_name="hf_target_base_bps", suggested_value=13_000,
                rationale="Realised volatility has risen against the signed band.",
            )
        return await original(self, payload)

    _StructuredFake.ainvoke = _dispatch  # type: ignore[method-assign]
    try:
        result = await _run(runtime, service, llm)
    finally:
        _StructuredFake.ainvoke = original  # type: ignore[method-assign]

    assert result.terminal == "tuning_suggested"
    assert result.tuning_id is not None
    assert result.proposal_id is None
    assert await runtime.store.list_proposals() == []

    tuning = await runtime.store.list_tuning()
    assert tuning[0].field_name == "hf_target_base_bps"
    assert tuning[0].suggested_value == 13_000
    assert tuning[0].requires_new_signature is True
    # FR-21: the registered mandate is untouched; only the borrower can change it.
    registered = service.params_of(BORROWER)
    assert registered is not None
    assert registered[0].hf_target_base_bps == _params().hf_target_base_bps


async def test_an_unregistered_borrower_ends_the_run_cleanly(runtime: AgentRuntime) -> None:
    service = make_service()
    service.unregister(BORROWER)
    result = await _run(runtime, service, FakeLLM(structured=_choice("protect_now")))
    assert result.terminal == "no_action"
    assert result.proposal_id is None
    assert result.facts == {}


# --- Degradation ------------------------------------------------------------------------------------


async def test_a_model_outage_still_produces_a_deterministic_run(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    """A Gemini outage must cost a narrative sentence, not the assessment and gate verdict."""
    result = await _run(runtime, service, FakeLLM(fail=True))

    assert result.terminal == "proposed"          # the facts still imply action
    assert result.strategy == "protect_now"       # from the deterministic fallback
    assert result.gate is not None and result.gate.allowed
    assert result.proposal_id is not None
    assert result.summary                          # a fallback summary, not an empty string


async def test_the_deterministic_fallback_stands_down_on_a_healthy_position(
    runtime: AgentRuntime
) -> None:
    service = make_service(hf=1.80)
    result = await _run(runtime, service, FakeLLM(fail=True))
    assert result.strategy == "stand_down"
    assert result.terminal == "no_action"


# --- Pure routing -------------------------------------------------------------------------------------


def test_route_strategy_is_a_pure_function_of_state() -> None:
    from app.agent.graph import route_strategy

    assert route_strategy({"registered": True, "strategy": "protect_now"}) == "gate"
    assert route_strategy({"registered": True, "strategy": "retune"}) == "tune"
    assert route_strategy({"registered": True, "strategy": "watch"}) == "done"
    assert route_strategy({"registered": True, "strategy": "stand_down"}) == "done"
    assert route_strategy({"registered": False, "strategy": "protect_now"}) == "done"


def test_route_gate_only_proposes_on_an_allowed_gate() -> None:
    from app.agent.graph import route_gate

    assert route_gate({"gate": {"allowed": True}}) == "propose"
    assert route_gate({"gate": {"allowed": False}}) == "blocked"
    assert route_gate({"gate": None}) == "blocked"
    assert route_gate({}) == "blocked"


def test_the_strategist_output_schema_admits_no_numbers() -> None:
    """The model cannot express a repay amount even if it tries to."""
    from app.agent.graph import StrategyChoice

    fields = StrategyChoice.model_fields
    assert set(fields) == {"strategy", "rationale"}
    assert fields["rationale"].annotation is str
    with pytest.raises(ValidationError):
        StrategyChoice(strategy="repay_400_usdc", rationale="x")  # type: ignore[arg-type]


def test_no_synchronous_model_call_exists_in_the_agent_package() -> None:
    """A blocking `.invoke(` would stall the keeper's polling behind a third-party round trip."""
    from pathlib import Path

    from app.agent.llm import assert_async_only

    package = Path(__file__).resolve().parent.parent / "app" / "agent"
    offenders = assert_async_only(*package.glob("*.py"))
    assert offenders == [], f"synchronous model calls found: {offenders}"
