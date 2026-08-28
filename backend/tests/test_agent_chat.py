"""Chat-agent tests — the tool loop, provenance, persistence, and the failure paths.

The chat box is meant to genuinely answer questions, so the assertions are about whether the
*right tools actually ran against the live service* and whether the reply carries the provenance
to prove it — not merely that some text came back.

Uses the scripted :class:`FakeLLM` from ``test_agent_graph.py``; no network, no API key.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.agent._lazy import agent_stack_available
from app.agent.runtime import AgentRuntime
from app.agent.store import AgentStore
from app.config.settings import settings
from app.core.protection_service import ProtectionService
from tests.test_agent_graph import FakeAIMessage, FakeLLM, make_service
from tests.test_protection_service import BORROWER

pytestmark = pytest.mark.skipif(
    not agent_stack_available(), reason='the optional agent extra is not installed ([agent])'
)


def _call(name: str, call_id: str = "c1", **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id}


@pytest.fixture
async def runtime():  # type: ignore[no-untyped-def]
    store = AgentStore(":memory:")
    await store.connect()
    try:
        yield AgentRuntime(store=store, model="fake", api_key="fake", timeout_s=5.0)
    finally:
        await store.close()


@pytest.fixture
def service() -> ProtectionService:
    return make_service()


async def _chat(runtime: AgentRuntime, service: ProtectionService, llm: FakeLLM, **kw: Any):  # type: ignore[no-untyped-def]
    from app.agent.chat import run_chat

    kw.setdefault("thread_id", "t1")
    kw.setdefault("message", "why is this position at risk?")
    return await run_chat(runtime=runtime, service=service, llm=llm, **kw)


# --- The loop ------------------------------------------------------------------------------------


async def test_a_question_with_no_tool_call_answers_directly(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    reply = await _chat(runtime, service, FakeLLM(prose="FinaX protects Aave positions."))
    assert reply.reply == "FinaX protects Aave positions."
    assert reply.tool_calls == []
    assert reply.truncated is False


async def test_a_tool_call_runs_against_the_live_service(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    """'Why is this at risk' must cause a real assessment, not a canned answer."""
    llm = FakeLLM(script=[
        FakeAIMessage(tool_calls=[_call("t_assess", borrower=BORROWER)]),
        # Every figure here matches what t_assess actually returns for this fixture — the guard
        # rejects prose that does not, as the sibling test below shows.
        FakeAIMessage("Health factor 1.14 sits below its target of 1.25; a repay of "
                      "1000000000 debt-token units costs about 15 bps."),
    ])
    reply = await _chat(runtime, service, llm)

    assert [t.name for t in reply.tool_calls] == ["t_assess"]
    assert reply.tool_calls[0].ok is True
    assert reply.sources == ["t_assess"]
    # The figures came from the backend, and the guard could verify them.
    assert reply.facts["t_assess"]["hf"] == 1.14
    assert reply.facts["t_assess"]["viable"] is True
    assert reply.guard_flagged is False


async def test_several_tools_in_one_turn_are_all_dispatched(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    llm = FakeLLM(script=[
        FakeAIMessage(tool_calls=[
            _call("t_position_snapshot", "c1", borrower=BORROWER),
            _call("t_risk_signal", "c2", borrower=BORROWER),
        ]),
        FakeAIMessage("The position is being watched."),
    ])
    reply = await _chat(runtime, service, llm)
    assert sorted(t.name for t in reply.tool_calls) == ["t_position_snapshot", "t_risk_signal"]
    assert reply.sources == ["t_position_snapshot", "t_risk_signal"]


async def test_the_model_is_bound_only_to_read_only_tools(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    """The chat surface must not contain either proposal writer, let alone anything executing."""
    llm = FakeLLM(prose="ok")
    await _chat(runtime, service, llm)
    names = {t.name for t in llm.bound_tools}
    assert "t_propose_protection" not in names
    assert "t_propose_tuning" not in names
    assert not any(n.startswith(("t_protect", "t_submit", "t_execute")) for n in names)
    assert "t_assess" in names and "t_explain_revert" in names


async def test_an_unknown_tool_name_is_answered_not_raised(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    llm = FakeLLM(script=[
        FakeAIMessage(tool_calls=[_call("t_nonexistent")]),
        FakeAIMessage("I could not look that up."),
    ])
    reply = await _chat(runtime, service, llm)
    assert reply.tool_calls[0].ok is False
    assert reply.tool_calls[0].error == "unknown tool"
    assert reply.sources == []


async def test_a_failing_tool_is_reported_in_the_trace(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    llm = FakeLLM(script=[
        # t_explain_state takes `state`; passing the wrong argument makes the call raise.
        FakeAIMessage(tool_calls=[_call("t_explain_state", wrong_arg="x")]),
        FakeAIMessage("I could not look that up."),
    ])
    reply = await _chat(runtime, service, llm)
    assert reply.tool_calls[0].ok is False
    assert reply.tool_calls[0].error


async def test_the_tool_loop_is_capped_and_says_so(
    runtime: AgentRuntime, service: ProtectionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that never concludes is a cost and latency bug, not an open-ended request."""
    monkeypatch.setattr(settings, "agent_max_tool_loops", 3)
    llm = FakeLLM(script=[
        FakeAIMessage(tool_calls=[_call("t_metrics", f"c{i}")]) for i in range(10)
    ])
    reply = await _chat(runtime, service, llm)
    assert reply.truncated is True
    assert len(reply.tool_calls) == 3
    assert "ran out of tool steps" in reply.reply


# --- Provenance ------------------------------------------------------------------------------------


async def test_an_invented_figure_is_flagged_not_suppressed(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    """The reply still reaches the operator — marked, so the console can degrade its styling."""
    llm = FakeLLM(script=[
        FakeAIMessage(tool_calls=[_call("t_assess", borrower=BORROWER)]),
        FakeAIMessage("HF is 1.14 and the liquidation penalty avoided would be 7.5%."),
    ])
    reply = await _chat(runtime, service, llm)

    assert reply.guard_flagged is True
    assert "Unverified figures" in reply.reply
    assert "7.5" in reply.reply
    assert reply.reply.startswith("HF is 1.14")  # the original answer is preserved


async def test_a_reply_with_no_tool_calls_cannot_assert_figures(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    """With nothing looked up, there is no provenance for any number."""
    reply = await _chat(runtime, service, FakeLLM(prose="Your health factor is 1.42."))
    assert reply.guard_flagged is True


async def test_prose_without_figures_is_never_flagged(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    reply = await _chat(runtime, service, FakeLLM(prose="The keeper is watching this position."))
    assert reply.guard_flagged is False


# --- Persistence -------------------------------------------------------------------------------------


async def test_the_turn_is_persisted_with_its_trace(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    llm = FakeLLM(script=[
        FakeAIMessage(tool_calls=[_call("t_assess", borrower=BORROWER)]),
        FakeAIMessage("Health factor 1.14."),
    ])
    await _chat(runtime, service, llm, message="how is it doing?")

    history = await runtime.store.history("t1")
    assert [m.role for m in history] == ["user", "assistant"]
    assert history[0].content == "how is it doing?"
    assert history[1].tool_calls is not None
    assert history[1].tool_calls[0]["name"] == "t_assess"


async def test_history_is_replayed_into_the_next_turn(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    await _chat(runtime, service, FakeLLM(prose="First answer."), message="first question")
    llm = FakeLLM(prose="Second answer.")
    await _chat(runtime, service, llm, message="and now?")

    # The model sees the system prompt plus both prior turns and the new question.
    sent = llm.calls[0]
    contents = [getattr(m, "content", "") for m in sent]
    assert any("first question" in c for c in contents)
    assert any("First answer." in c for c in contents)
    assert any("and now?" in c for c in contents)


async def test_the_current_message_is_not_duplicated_in_the_prompt(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    llm = FakeLLM(prose="ok")
    await _chat(runtime, service, llm, message="only once please")
    contents = [getattr(m, "content", "") for m in llm.calls[0]]
    assert sum(1 for c in contents if c == "only once please") == 1


async def test_borrower_context_is_put_in_the_system_prompt(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    """So 'how am I doing?' resolves without the operator pasting an address."""
    llm = FakeLLM(prose="ok")
    await _chat(runtime, service, llm, message="how am I doing?", borrower=BORROWER)
    system = getattr(llm.calls[0][0], "content", "")
    assert BORROWER in system
    assert "Questions that do not name an address refer to it" in system


async def test_threads_are_isolated(runtime: AgentRuntime, service: ProtectionService) -> None:
    await _chat(runtime, service, FakeLLM(prose="a"), thread_id="t1", message="q1")
    await _chat(runtime, service, FakeLLM(prose="b"), thread_id="t2", message="q2")
    assert [m.content for m in await runtime.store.history("t1")] == ["q1", "a"]
    assert [m.content for m in await runtime.store.history("t2")] == ["q2", "b"]


# --- Degradation --------------------------------------------------------------------------------------


async def test_a_model_outage_answers_instead_of_raising(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    reply = await _chat(runtime, service, FakeLLM(fail=True))
    assert "could not reach the model" in reply.reply
    assert reply.tool_calls == []
    # Still persisted, so the operator's console shows what happened.
    assert len(await runtime.store.history("t1")) == 2


async def test_the_system_prompt_states_the_agents_limits(
    runtime: AgentRuntime, service: ProtectionService
) -> None:
    llm = FakeLLM(prose="ok")
    await _chat(runtime, service, llm)
    system = getattr(llm.calls[0][0], "content", "")
    assert "NOT the decision-maker" in system
    assert "no tool that submits a transaction" in system
    assert "NEVER invent" in system
