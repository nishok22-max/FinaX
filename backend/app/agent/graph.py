"""The crew graph (HEAVY — imported only from inside a request handler).

```
START -> monitor -> analyst -> strategist -> route
                                              |-- protect_now -> policy_gate -> [allowed] -> propose -> auditor
                                              |                               \\-[blocked] ----------> auditor
                                              |-- retune ------> tuner ---------------------------> auditor
                                              \\-- else ------------------------------------------> auditor -> END
```

Which nodes may write which parts of the state is the design:

* ``monitor``, ``policy_gate`` and ``propose`` run **no model at all**. They read the chain, run
  the deterministic gate, and persist a row.
* ``analyst``, ``strategist``, ``tuner`` and ``auditor`` call the model — but only ever to produce
  *prose* and *one constrained enum*. `StrategyChoice.strategy` is a `Literal`, so the model
  cannot express a repay amount even if it wanted to.
* Every number in ``state["facts"]`` is written by :mod:`app.agent.facts` from backend responses.

**Compiled without a checkpointer, and with no human-in-the-loop interrupt.** LangGraph's
``interrupt()`` re-executes the whole node on resume, which is unsafe for anything with side
effects, and — more fundamentally — routing approval through an HTTP endpoint means the graph
holds no execution capability at all. The agent's inability to submit is then a fact about the
code rather than a property of how carefully the graph was wired.
"""
from __future__ import annotations

import logging
import time
import uuid
from functools import partial
from typing import Any, TypedDict

from pydantic import BaseModel, Field

from app.agent import facts as facts_mod
from app.agent import policy, tools
from app.agent.errors import AgentError
from app.agent.guard import NumberGuard
from app.agent.models import (
    AuditAction,
    CrewRunResult,
    PolicyDecision,
    Strategy,
    Terminal,
    TunableField,
)
from app.agent.runtime import AgentRuntime
from app.core.protection_service import ProtectionService

logger = logging.getLogger(__name__)

_HOUR = 3600.0


class StrategyChoice(BaseModel):
    """The strategist's entire output surface.

    A constrained enum plus prose. There is no numeric field, so no model output can become an
    argument to anything that moves value.
    """

    strategy: Strategy
    rationale: str = Field(min_length=1, max_length=1200)


class TuningChoice(BaseModel):
    """The tuner's output — one mandate field and a new value, for the borrower to sign."""

    field_name: TunableField
    suggested_value: int = Field(ge=0, le=1_000_000)
    rationale: str = Field(min_length=1, max_length=1200)


class CrewState(TypedDict, total=False):
    """Graph state, partitioned by who may write it.

    Inputs and everything numeric are written by Python; the model writes only the ``*_note`` /
    ``*_narrative`` / ``strategy`` / ``*_rationale`` keys.
    """

    # Inputs — set by the caller.
    borrower: str
    trigger: str
    run_id: str

    # Deterministic facts — produced by code only.
    facts: dict[str, Any]
    registered: bool
    debt_price_base: int
    debt_decimals: int

    # Model output — prose and one constrained enum.
    risk_narrative: str
    strategy: str
    strategy_rationale: str
    summary: str

    # Gate, persistence and outcome.
    gate: dict[str, Any] | None
    proposal_id: int | None
    tuning_id: int | None
    guard_flagged: bool
    error: str | None
    terminal: str


async def _record(
    rt: AgentRuntime, state: CrewState, node: str, output: dict[str, Any],
    *, started: float, llm_used: bool = False, error: str | None = None,
) -> None:
    """Persist one node's execution as a reasoning-trace row."""
    try:
        await rt.store.insert_decision(
            run_id=state["run_id"], borrower=state["borrower"], node=node, output=output,
            latency_ms=int((time.perf_counter() - started) * 1000),
            llm_used=llm_used, error=error,
        )
    except Exception:  # a trace-write failure must not abort the run
        logger.warning("could not record the %s node's trace", node, exc_info=True)


async def _prose(
    rt: AgentRuntime, llm: Any, system: str, payload: dict[str, Any], *, fallback: str
) -> str:
    """One prose turn, degrading to ``fallback`` rather than failing the run.

    A model outage should cost the operator a narrative sentence, not the deterministic
    assessment and gate verdict that the run actually exists to produce.
    """
    from app.agent._lazy import lc_messages
    from app.agent.llm import ainvoke_guarded, text_of

    msgs = lc_messages()
    try:
        reply = await ainvoke_guarded(
            llm,
            [msgs.SystemMessage(content=system), msgs.HumanMessage(content=repr(payload))],
            timeout_s=rt.timeout_seconds,
        )
        text = text_of(getattr(reply, "content", ""))
        return text if text.strip() else fallback
    except Exception:
        logger.warning("model call failed; falling back to a deterministic summary", exc_info=True)
        return fallback


# --- Nodes -------------------------------------------------------------------------------------


async def node_monitor(
    state: CrewState, *, service: ProtectionService, rt: AgentRuntime
) -> dict[str, Any]:
    """Read the position and assemble the fact sheet. No model involvement whatsoever."""
    started = time.perf_counter()
    borrower = state["borrower"]
    registered = service.params_of(borrower)

    if registered is None:
        out: dict[str, Any] = {
            "registered": False, "terminal": "no_action",
            "error": "borrower has no registered signed mandate",
        }
        await _record(rt, state, "monitor", out, started=started)
        return out

    params, _ = registered
    assessment = await service.assess(params, sigma=tools.sigma_for(service, params))
    snapshot = await service.snapshot(borrower, params.hf_trigger_bps)
    signal = tools.risk_signal(service, params, snapshot.hf)
    debt_asset, debt_decimals = tools.debt_asset_facts()

    sheet = facts_mod.build_factsheet(
        snapshot=snapshot, assessment=assessment, risk=signal, debt_asset=debt_asset
    )
    out = {
        "registered": True,
        "facts": sheet.model_dump(),
        "debt_decimals": debt_decimals,
        "debt_price_base": await _debt_price_base(debt_asset),
    }
    await _record(rt, state, "monitor", {"state": sheet.state, "hf": sheet.hf},
                  started=started)
    return out


async def node_analyst(
    state: CrewState, *, rt: AgentRuntime, llm: Any
) -> dict[str, Any]:
    """Narrate the risk picture from the fact sheet."""
    from app.agent.prompts import ANALYST_SYSTEM

    started = time.perf_counter()
    sheet = state.get("facts", {})
    fallback = (
        f"Health factor {sheet.get('hf')} against a target of {sheet.get('hf_target')}; "
        f"state {sheet.get('state')}."
    )
    narrative = await _prose(rt, llm, ANALYST_SYSTEM, sheet, fallback=fallback)
    out = {"risk_narrative": narrative}
    await _record(rt, state, "analyst", out, started=started, llm_used=True)
    return out


async def node_strategist(
    state: CrewState, *, rt: AgentRuntime, llm: Any
) -> dict[str, Any]:
    """Choose one course of action from a fixed enumeration."""
    from app.agent._lazy import lc_messages
    from app.agent.llm import ainvoke_guarded
    from app.agent.prompts import STRATEGIST_SYSTEM

    started = time.perf_counter()
    sheet = state.get("facts", {})
    payload = {"facts": sheet, "analysis": state.get("risk_narrative", "")}

    choice = _deterministic_strategy(sheet)
    rationale = "Chosen from the fact sheet without a model."
    try:
        msgs = lc_messages()
        structured = llm.with_structured_output(StrategyChoice)
        reply = await ainvoke_guarded(
            structured,
            [msgs.SystemMessage(content=STRATEGIST_SYSTEM),
             msgs.HumanMessage(content=repr(payload))],
            timeout_s=rt.timeout_seconds,
        )
        parsed = reply if isinstance(reply, StrategyChoice) else StrategyChoice.model_validate(reply)
        choice, rationale = parsed.strategy, parsed.rationale
    except Exception:
        logger.warning("strategist model call failed; using the deterministic choice",
                       exc_info=True)

    out = {"strategy": choice, "strategy_rationale": rationale}
    await _record(rt, state, "strategist", out, started=started, llm_used=True)
    return out


async def node_policy_gate(
    state: CrewState, *, service: ProtectionService, rt: AgentRuntime
) -> dict[str, Any]:
    """Re-derive the gate's inputs from the backend and run it. Pure and LLM-free."""
    started = time.perf_counter()
    borrower = state["borrower"]
    registered = service.params_of(borrower)
    if registered is None:
        out: dict[str, Any] = {"gate": None, "terminal": "gate_blocked",
                               "error": "mandate disappeared mid-run"}
        await _record(rt, state, "policy_gate", out, started=started)
        return out

    params, _ = registered
    assessment = await service.assess(params, sigma=tools.sigma_for(service, params))
    snapshot = await service.snapshot(borrower, params.hf_trigger_bps)
    since = time.time() - _HOUR
    decision = policy.evaluate(
        params=params, assessment=assessment, snapshot=snapshot, metrics=service.metrics(),
        limits=rt.limits(), now=time.time(),
        debt_price_base=state.get("debt_price_base", 0),
        debt_decimals=state.get("debt_decimals", 6),
        recent_proposals_borrower=await rt.store.count_recent_proposals(
            since=since, borrower=borrower),
        recent_proposals_global=await rt.store.count_recent_proposals(since=since),
    )
    out = {"gate": decision.model_dump()}
    await _record(rt, state, "policy_gate",
                  {"allowed": decision.allowed, "blocking": decision.blocking}, started=started)
    return out


async def node_propose(state: CrewState, *, rt: AgentRuntime) -> dict[str, Any]:
    """Queue a proposal for human approval. Writes a row; submits nothing."""
    started = time.perf_counter()
    gate = PolicyDecision.model_validate(state["gate"])

    # Check the narrative's figures against the fact sheet before it is stored and rendered.
    guard = NumberGuard()
    guard.observe(state.get("facts", {}))
    rationale, flagged, _ = guard.annotate(state.get("strategy_rationale", ""))

    proposal_id = await tools.t_propose_protection(
        rt.store, run_id=state["run_id"], borrower=state["borrower"],
        facts=state.get("facts", {}), rationale=rationale, gate=gate,
        ttl_seconds=rt.limits().proposal_ttl_seconds, guard_flagged=flagged,
    )
    await rt.store.audit(
        actor="agent", action=AuditAction.PROPOSED, borrower=state["borrower"],
        proposal_id=proposal_id, detail={"strategy": "protect_now", "run_id": state["run_id"]},
    )
    out = {"proposal_id": proposal_id, "guard_flagged": flagged, "terminal": "proposed"}
    await _record(rt, state, "propose", out, started=started)
    return out


async def node_tuner(
    state: CrewState, *, service: ProtectionService, rt: AgentRuntime, llm: Any
) -> dict[str, Any]:
    """Raise a re-sign request for one mandate field (FR-21). Changes nothing by itself."""
    from app.agent._lazy import lc_messages
    from app.agent.llm import ainvoke_guarded
    from app.agent.prompts import TUNER_SYSTEM

    started = time.perf_counter()
    registered = service.params_of(state["borrower"])
    if registered is None:
        out: dict[str, Any] = {"terminal": "no_action", "error": "mandate disappeared mid-run"}
        await _record(rt, state, "tuner", out, started=started)
        return out
    params, _ = registered

    try:
        msgs = lc_messages()
        structured = llm.with_structured_output(TuningChoice)
        reply = await ainvoke_guarded(
            structured,
            [msgs.SystemMessage(content=TUNER_SYSTEM),
             msgs.HumanMessage(content=repr({
                 "facts": state.get("facts", {}),
                 "current_mandate": {
                     "hf_trigger_bps": params.hf_trigger_bps,
                     "hf_target_base_bps": params.hf_target_base_bps,
                     "hf_target_max_bps": params.hf_target_max_bps,
                     "vol_coeff_k": params.vol_coeff_k,
                     "max_slippage_bps": params.max_slippage_bps,
                     "max_cost_bps": params.max_cost_bps,
                 },
             }))],
            timeout_s=rt.timeout_seconds,
        )
        choice = reply if isinstance(reply, TuningChoice) else TuningChoice.model_validate(reply)
        tuning_id = await tools.t_propose_tuning(
            rt.store, run_id=state["run_id"], borrower=state["borrower"], params=params,
            field_name=choice.field_name, suggested_value=choice.suggested_value,
            rationale=choice.rationale,
        )
    except Exception as exc:
        logger.warning("tuner produced no usable suggestion", exc_info=True)
        out = {"terminal": "no_action", "error": f"no usable tuning suggestion: {exc}"}
        await _record(rt, state, "tuner", out, started=started, llm_used=True,
                      error=str(exc))
        return out

    await rt.store.audit(
        actor="agent", action=AuditAction.TUNING_SUGGESTED, borrower=state["borrower"],
        detail={"tuning_id": tuning_id, "field": choice.field_name,
                "suggested_value": choice.suggested_value},
    )
    out = {"tuning_id": tuning_id, "terminal": "tuning_suggested"}
    await _record(rt, state, "tuner", out, started=started, llm_used=True)
    return out


async def node_auditor(
    state: CrewState, *, rt: AgentRuntime, llm: Any
) -> dict[str, Any]:
    """Write the one-sentence activity-feed summary and close the run out."""
    from app.agent.prompts import AUDITOR_SYSTEM

    started = time.perf_counter()
    gate = state.get("gate")
    terminal: Terminal = state.get("terminal") or _terminal_for(state)  # type: ignore[assignment]

    payload = {
        "facts": state.get("facts", {}),
        "strategy": state.get("strategy"),
        "rationale": state.get("strategy_rationale"),
        "gate_allowed": (gate or {}).get("allowed"),
        "gate_blocking": (gate or {}).get("blocking"),
        "terminal": terminal,
    }
    fallback = _fallback_summary(state, terminal)
    summary = await _prose(rt, llm, AUDITOR_SYSTEM, payload, fallback=fallback)

    guard = NumberGuard()
    guard.observe(state.get("facts", {}))
    summary, flagged, _ = guard.annotate(summary)

    if terminal == "gate_blocked":
        await rt.store.audit(
            actor="agent", action=AuditAction.GATE_BLOCKED, borrower=state["borrower"],
            detail={"blocking": (gate or {}).get("blocking", []), "run_id": state["run_id"]},
        )
    await rt.store.audit(
        actor="agent", action=AuditAction.CREW_RUN, borrower=state["borrower"],
        detail={"run_id": state["run_id"], "terminal": terminal,
                "strategy": state.get("strategy")},
    )
    out = {"summary": summary, "terminal": terminal,
           "guard_flagged": state.get("guard_flagged", False) or flagged}
    await _record(rt, state, "auditor", {"terminal": terminal}, started=started,
                  llm_used=True)
    return out


# --- Routing (pure) ------------------------------------------------------------------------------


def route_strategy(state: CrewState) -> str:
    if not state.get("registered", False):
        return "done"
    strategy = state.get("strategy")
    if strategy == "protect_now":
        return "gate"
    if strategy == "retune":
        return "tune"
    return "done"


def route_gate(state: CrewState) -> str:
    gate = state.get("gate")
    return "propose" if gate and gate.get("allowed") else "blocked"


def _deterministic_strategy(sheet: dict[str, Any]) -> Strategy:
    """The choice the facts alone imply — the fallback when the model is unavailable.

    Not a shadow decision-maker: the policy gate re-checks whatever comes out of here just as it
    checks the model's choice. It exists so a Gemini outage degrades the crew to a conservative
    deterministic run instead of failing it.
    """
    if not sheet:
        return "stand_down"
    hf_bps = int(sheet.get("hf_bps", 0))
    trigger = int(sheet.get("hf_trigger_bps", 0))
    if sheet.get("viable") and hf_bps <= trigger:
        return "protect_now"
    if hf_bps <= trigger + 500:
        return "watch"
    return "stand_down"


def _terminal_for(state: CrewState) -> Terminal:
    if state.get("proposal_id"):
        return "proposed"
    if state.get("tuning_id"):
        return "tuning_suggested"
    gate = state.get("gate")
    if gate is not None and not gate.get("allowed"):
        return "gate_blocked"
    return "no_action"


def _fallback_summary(state: CrewState, terminal: str) -> str:
    borrower = state["borrower"]
    sheet = state.get("facts", {})
    if terminal == "proposed":
        return (f"Queued a rescue proposal for {borrower}: HF {sheet.get('hf')} is at or below "
                f"its trigger and the pipeline judged the intervention viable.")
    if terminal == "gate_blocked":
        blocking = ", ".join((state.get("gate") or {}).get("blocking", [])) or "unknown"
        return f"Policy gate refused an intervention for {borrower}; failed checks: {blocking}."
    if terminal == "tuning_suggested":
        return f"Raised a mandate re-sign request for {borrower}."
    return f"No action warranted for {borrower} (state {sheet.get('state', 'unknown')})."


async def _debt_price_base(debt_asset: str) -> int:
    """Oracle price of the debt asset; 0 on failure, which fails the repay bound closed."""
    try:
        from app.chain.oracle import OracleClient

        return (await OracleClient().get_asset_price(debt_asset)).price
    except Exception:  # an unreadable oracle blocks the gate rather than aborting the run
        logger.warning("could not read the debt-asset price; repay bound will fail closed",
                       exc_info=True)
        return 0


# --- Assembly ------------------------------------------------------------------------------------


def build_crew_graph(
    *, rt: AgentRuntime, service: ProtectionService, llm: Any
) -> Any:
    """Compile the crew graph. No checkpointer, no interrupt — see the module docstring.

    Note the parameter name: nodes take ``rt``, never ``runtime``. LangGraph 1.x injects its own
    ``Runtime`` object into any node declaring a ``runtime`` parameter, which silently shadowed
    the partial-bound one and surfaced as ``NoneType has no attribute 'store'`` deep inside a
    node. Keep them distinct.
    """
    from app.agent._lazy import langgraph_graph

    g = langgraph_graph()
    builder = g.StateGraph(CrewState)
    builder.add_node("monitor", partial(node_monitor, service=service, rt=rt))
    builder.add_node("analyst", partial(node_analyst, rt=rt, llm=llm))
    builder.add_node("strategist", partial(node_strategist, rt=rt, llm=llm))
    builder.add_node("policy_gate", partial(node_policy_gate, service=service, rt=rt))
    builder.add_node("propose", partial(node_propose, rt=rt))
    builder.add_node("tuner", partial(node_tuner, service=service, rt=rt, llm=llm))
    builder.add_node("auditor", partial(node_auditor, rt=rt, llm=llm))

    builder.add_edge(g.START, "monitor")
    builder.add_edge("monitor", "analyst")
    builder.add_edge("analyst", "strategist")
    builder.add_conditional_edges(
        "strategist", route_strategy,
        {"gate": "policy_gate", "tune": "tuner", "done": "auditor"},
    )
    builder.add_conditional_edges(
        "policy_gate", route_gate, {"propose": "propose", "blocked": "auditor"},
    )
    builder.add_edge("propose", "auditor")
    builder.add_edge("tuner", "auditor")
    builder.add_edge("auditor", g.END)
    return builder.compile()


async def run_crew(
    *,
    runtime: AgentRuntime,
    service: ProtectionService,
    borrower: str,
    trigger: str = "manual",
    llm: Any | None = None,
) -> CrewRunResult:
    """Run the crew for one borrower.

    ``llm`` is injectable so tests drive the whole graph — topology, gate, persistence, routing —
    with a fake model and no network.
    """
    from app.agent.llm import build_chat_model

    model = llm if llm is not None else build_chat_model(
        model=runtime.model, api_key=runtime.api_key
    )
    graph = build_crew_graph(rt=runtime, service=service, llm=model)
    run_id = uuid.uuid4().hex

    initial: CrewState = {"borrower": borrower, "trigger": trigger, "run_id": run_id}
    try:
        final: dict[str, Any] = await graph.ainvoke(initial)
    except AgentError:
        raise
    except Exception as exc:
        logger.exception("crew run failed for borrower=%s", borrower)
        return CrewRunResult(
            run_id=run_id, borrower=borrower, terminal="error", strategy="stand_down",
            summary=f"The crew run failed: {exc}", facts={},
        )

    gate_payload = final.get("gate")
    return CrewRunResult(
        run_id=run_id,
        borrower=borrower,
        terminal=final.get("terminal", "no_action"),
        strategy=final.get("strategy", "stand_down"),
        summary=final.get("summary", ""),
        facts=final.get("facts", {}),
        gate=PolicyDecision.model_validate(gate_payload) if gate_payload else None,
        proposal_id=final.get("proposal_id"),
        tuning_id=final.get("tuning_id"),
        guard_flagged=final.get("guard_flagged", False),
        trace=await runtime.store.list_decisions(run_id),
    )
