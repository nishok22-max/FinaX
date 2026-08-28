"""Pydantic types for the agent layer — persistence rows, the policy verdict, and the wire API.

Three families, kept in one module because they are all declarative and all need to agree:

  * **Gate types** (:class:`GateCheck`, :class:`PolicyDecision`, :class:`PolicyLimits`) — the
    verdict of the deterministic gate in :mod:`app.agent.policy`. They live here rather than in
    ``policy.py`` so that the store and the routes can round-trip a decision without importing
    the gate logic.
  * **Row models** — the typed shape of what :mod:`app.agent.store` reads back out of SQLite.
  * **Wire models** — request/response bodies for ``/agent/*``.

Nothing here imports the optional agent stack; this module is safe to import anywhere.

Note on numbers: :class:`ChatReply` and :class:`ProposalOut` carry ``facts`` and ``sources``
alongside the model's prose precisely so a renderer never has to parse a figure out of English
(FR-22). ``rationale`` and ``reply`` are narrative; ``facts`` is data.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.models import ProtectResponse

# --- Enumerations --------------------------------------------------------------------------


class ProposalStatus(str, Enum):
    """Lifecycle of an agent proposal.

    ``STALE`` and ``EXPIRED`` are distinct on purpose: ``EXPIRED`` means nobody looked in time,
    ``STALE`` means somebody did and the position had moved underneath it (FR-19). They call for
    different operator responses, so they are not collapsed into one status.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"    # human said yes; execution in progress
    REJECTED = "REJECTED"    # human said no
    EXECUTED = "EXECUTED"    # protect() ran and the tx succeeded
    FAILED = "FAILED"        # protect() ran and the tx reverted / errored
    STALE = "STALE"          # gate re-evaluation failed at approval time
    EXPIRED = "EXPIRED"      # TTL elapsed before anyone decided


class TuningStatus(str, Enum):
    OPEN = "open"
    DISMISSED = "dismissed"
    RESIGNED = "resigned"    # borrower signed the suggested mandate


class AuditAction(str, Enum):
    """Append-only audit vocabulary. Stable strings — the console renders them verbatim."""

    CREW_RUN = "CREW_RUN"
    PROPOSED = "PROPOSED"
    GATE_BLOCKED = "GATE_BLOCKED"
    TUNING_SUGGESTED = "TUNING_SUGGESTED"
    TUNING_DISMISSED = "TUNING_DISMISSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    STALE_ON_APPROVE = "STALE_ON_APPROVE"
    EXPIRED_ON_APPROVE = "EXPIRED_ON_APPROVE"
    PANIC = "PANIC"


Strategy = Literal["protect_now", "watch", "stand_down", "retune"]
Terminal = Literal["proposed", "no_action", "gate_blocked", "tuning_suggested", "error"]

#: Fields of ``RiskParams`` a tuning suggestion is allowed to touch. Excludes ``borrower``,
#: ``allowed_collaterals``, ``nonce`` and ``deadline`` — changing those is a different action
#: with different consequences, not a risk-appetite tweak.
TunableField = Literal[
    "hf_trigger_bps",
    "hf_target_base_bps",
    "hf_target_max_bps",
    "vol_coeff_k",
    "max_slippage_bps",
    "max_cost_bps",
]


# --- Policy gate ---------------------------------------------------------------------------


class GateCheck(BaseModel):
    """One named check and its outcome. ``detail`` is rendered to the operator verbatim."""

    name: str
    passed: bool
    detail: str


class PolicyDecision(BaseModel):
    """The gate's full verdict.

    Every check is reported, passed or not — the console renders the complete checklist, which is
    what makes the safety architecture legible at a glance. ``blocking`` is the failed subset,
    duplicated for convenience.
    """

    allowed: bool
    checks: list[GateCheck]
    blocking: list[str] = Field(default_factory=list)
    severity: Literal["ok", "soft_block", "hard_block"] = "ok"


class PolicyLimits(BaseModel):
    """Operator-side ceilings, applied *on top of* the borrower's signed mandate.

    The borrower's ``RiskParams`` are the outer bound; these are the keeper's own, stricter one.
    Where both cover the same quantity (cost), the stricter wins.
    """

    max_repay_fraction_of_debt: float = Field(default=0.50, gt=0.0, le=1.0)
    max_cost_bps_ceiling: int = Field(default=200, ge=0)
    min_hf_gap_bps: int = Field(default=25, ge=0)
    max_proposals_per_borrower_per_hour: int = Field(default=3, ge=0)
    max_proposals_global_per_hour: int = Field(default=12, ge=0)
    proposal_ttl_seconds: int = Field(default=900, ge=1)


# --- Persistence rows ----------------------------------------------------------------------


class ProposalRow(BaseModel):
    """A row of ``proposals`` as read back from SQLite."""

    id: int
    run_id: str
    borrower: str
    created_at: float
    expires_at: float
    status: ProposalStatus
    strategy: str
    facts: dict[str, Any]
    gate: PolicyDecision
    rationale: str
    guard_flagged: bool = False
    decided_at: float | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    tx_hash: str | None = None
    result: dict[str, Any] | None = None


class TuningRow(BaseModel):
    """A row of ``tuning_suggestions`` — a *re-sign request*, never an applied change (FR-21)."""

    id: int
    run_id: str
    borrower: str
    created_at: float
    field_name: TunableField
    current_value: int
    suggested_value: int
    rationale: str
    #: The complete new ``RiskParams`` the borrower must sign for this to take effect.
    eip712_payload: dict[str, Any]
    status: TuningStatus = TuningStatus.OPEN
    decided_at: float | None = None
    #: Structurally always true: the vault re-verifies the EIP-712 signature, so a mandate the
    #: borrower has not signed cannot execute. Modelled as a constant so no code path can unset it.
    requires_new_signature: Literal[True] = True


class AuditRow(BaseModel):
    id: int
    ts: float
    actor: Literal["agent", "human", "system"]
    action: AuditAction
    borrower: str | None = None
    proposal_id: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ChatMessageRow(BaseModel):
    id: int
    thread_id: str
    ts: float
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    facts: dict[str, Any] | None = None
    guard_flagged: bool = False


class DecisionRow(BaseModel):
    """One node execution inside a crew run — the reasoning trace, per node."""

    id: int
    run_id: str
    borrower: str
    created_at: float
    node: str
    latency_ms: int | None = None
    llm_used: bool = False
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


# --- Wire models ---------------------------------------------------------------------------


class AgentStatusResponse(BaseModel):
    """Always returned with 200, even when the layer is off — the console polls it to decide
    whether to render the composer, and an error there would look like a broken page."""

    enabled: bool
    reason: str | None = None
    model: str | None = None
    stack_available: bool = False
    store_ready: bool = False
    pending_proposals: int = 0


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    #: Omit to have the server mint one; the console then reuses it for the conversation.
    thread_id: str | None = None
    #: Console context, so "how am I doing?" resolves without the user pasting an address.
    borrower: str | None = None


class ToolCallTrace(BaseModel):
    """What the model actually looked at. Rendered so an operator can audit the answer."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    latency_ms: int = 0
    error: str | None = None


class ChatReply(BaseModel):
    thread_id: str
    reply: str
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    #: Provenance for every number in ``reply`` (FR-22).
    facts: dict[str, Any] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)
    #: True when a figure in ``reply`` could not be traced to a tool result. The console renders
    #: these in a degraded style rather than in the live-data style.
    guard_flagged: bool = False
    #: True when the tool loop hit its cap before the model finished.
    truncated: bool = False


class ChatHistory(BaseModel):
    thread_id: str
    messages: list[ChatMessageRow]


class CrewRunRequest(BaseModel):
    borrower: str
    trigger: Literal["manual", "worker"] = "manual"


class CrewRunResult(BaseModel):
    run_id: str
    borrower: str
    terminal: Terminal
    strategy: str
    summary: str
    facts: dict[str, Any] = Field(default_factory=dict)
    gate: PolicyDecision | None = None
    proposal_id: int | None = None
    tuning_id: int | None = None
    guard_flagged: bool = False
    trace: list[DecisionRow] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    approved_by: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    #: Approving a proposal past its TTL requires saying so explicitly. The gate still re-runs;
    #: this only overrides the age check, never a safety check.
    acknowledge_stale: bool = False


class RejectRequest(BaseModel):
    rejected_by: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)


class ApprovalResult(BaseModel):
    proposal: ProposalRow
    #: The gate as re-evaluated against a *fresh* assessment at approval time, not the one stored
    #: when the proposal was created (FR-19).
    revalidated_gate: PolicyDecision
    #: The existing, unchanged keeper response model — approval runs the identical code path as
    #: ``POST /positions/{borrower}/protect``.
    protect: ProtectResponse | None = None
    reason: str | None = None
