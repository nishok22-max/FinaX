"""Adapter from :mod:`app.agent.tools` to LangChain tool specs (HEAVY).

The only file that needs a LangChain import in order to describe the agent's capabilities. Keeping
the adapter separate from the bodies means the whole tool surface stays testable without the
optional stack installed, and it keeps the *authoritative* list of what the model may do — this
module's :func:`read_only_specs` — in one readable place.

Only read-only tools appear here. The two proposal writers in ``tools.py`` are called directly by
the crew graph after the deterministic gate has passed; they are never bound to a model, so no
model can invoke them at a moment of its own choosing.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agent import tools
from app.agent.store import AgentStore
from app.core.protection_service import ProtectionService


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool, with the JSON schema the model sees."""

    name: str
    description: str
    parameters: dict[str, Any]
    call: Callable[..., Awaitable[Any]]


def _schema(**properties: dict[str, Any]) -> dict[str, Any]:
    required = [n for n, p in properties.items() if not p.pop("optional", False)]
    return {"type": "object", "properties": properties, "required": required}


_BORROWER = {"type": "string", "description": "The borrower's 0x address."}


def read_only_specs(service: ProtectionService, store: AgentStore) -> list[ToolSpec]:
    """The chat agent's complete capability surface. Nothing here can write or submit."""
    return [
        ToolSpec(
            "t_doctrine",
            "What FinaX is, how it decides, and where the agent's authority ends. Call this "
            "when asked what the system is or what you are allowed to do.",
            _schema(),
            lambda: tools.t_doctrine(),
        ),
        ToolSpec(
            "t_list_positions",
            "List the borrower addresses with a registered signed mandate (the positions the "
            "keeper is watching).",
            _schema(),
            lambda: tools.t_list_positions(service),
        ),
        ToolSpec(
            "t_position_snapshot",
            "Live account data for a borrower: health factor, collateral and debt in USD, "
            "liquidation threshold, lifecycle state, and whether they are registered.",
            _schema(borrower=dict(_BORROWER)),
            lambda borrower: tools.t_position_snapshot(service, borrower),
        ),
        ToolSpec(
            "t_position_state",
            "The borrower's current lifecycle state and what that state means.",
            _schema(borrower=dict(_BORROWER)),
            lambda borrower: tools.t_position_state(service, borrower),
        ),
        ToolSpec(
            "t_registered_params",
            "The borrower's signed risk mandate: trigger, target band, slippage and cost caps, "
            "and the allowed collaterals.",
            _schema(borrower=dict(_BORROWER)),
            lambda borrower: tools.t_registered_params(service, borrower),
        ),
        ToolSpec(
            "t_assess",
            "Run the decision pipeline for a registered borrower and return the authoritative "
            "numbers: health factor, target, repay amount, chosen collateral, estimated cost in "
            "bps, whether the intervention is viable, and the reason if not. Read-only.",
            _schema(borrower=dict(_BORROWER)),
            lambda borrower: tools.t_assess(service, borrower),
        ),
        ToolSpec(
            "t_risk_signal",
            "Realised volatility (sigma), the heuristic breach probability, and the dynamic "
            "health-factor target derived from them. Use for 'how volatile' or 'how likely' "
            "questions.",
            _schema(borrower=dict(_BORROWER)),
            lambda borrower: tools.t_risk_signal(service, borrower),
        ),
        ToolSpec(
            "t_metrics",
            "Keeper health: circuit-breaker state, in-flight locks, decision counters, and the "
            "per-borrower state map.",
            _schema(),
            lambda: tools.t_metrics(service),
        ),
        ToolSpec(
            "t_explain_state",
            "Explain a PositionState (HEALTHY, WATCH, ASSESSING, DECLINED, READY, SUBMITTED, "
            "RESTORED, REVERTED) and which states it may move to.",
            _schema(state={"type": "string", "description": "The state name."}),
            lambda state: tools.t_explain_state(state),
        ),
        ToolSpec(
            "t_explain_revert",
            "Explain one of the vault's custom errors (e.g. HealthBelowTarget, BadSignature, "
            "CostExceeded) in the contract's own terms.",
            _schema(error_name={"type": "string",
                                "description": "The Solidity custom-error name."}),
            lambda error_name: tools.t_explain_revert(error_name),
        ),
        ToolSpec(
            "t_list_proposals",
            "Agent proposals and their approval status.",
            _schema(
                borrower={**_BORROWER, "optional": True},
                status={"type": "string", "optional": True,
                        "description": "PENDING, APPROVED, EXECUTED, REJECTED, STALE, EXPIRED, "
                                       "or FAILED."},
            ),
            lambda borrower=None, status=None: tools.t_list_proposals(
                store, borrower=borrower, status=status
            ),
        ),
        ToolSpec(
            "t_audit_trail",
            "What the agent and its operators have done recently, newest first.",
            _schema(borrower={**_BORROWER, "optional": True}),
            lambda borrower=None: tools.t_audit_trail(store, borrower=borrower),
        ),
    ]


def as_langchain_tools(specs: list[ToolSpec]) -> list[Any]:
    """Wrap specs as ``StructuredTool``s for ``llm.bind_tools``."""
    from app.agent._lazy import lc_tools

    structured = lc_tools().StructuredTool
    return [
        structured.from_function(
            coroutine=spec.call, name=spec.name, description=spec.description,
            args_schema=spec.parameters,
        )
        for spec in specs
    ]
