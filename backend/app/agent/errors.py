"""Exception types for the agent layer.

All of these are *expected* conditions the routes translate into HTTP status codes, not bugs:
the layer is optional and talks to a third-party model over the network, so "unavailable" and
"too slow" are ordinary outcomes that must never surface as a 500.
"""
from __future__ import annotations


class AgentError(RuntimeError):
    """Base class for every agent-layer failure."""


class AgentDisabled(AgentError):
    """The layer is off, unconfigured, or its optional dependencies are absent.

    Carries the specific reason so the operator is told *which* precondition failed rather than
    a generic "disabled" — the difference between a five-second fix and a confused demo.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AgentTimeout(AgentError):
    """The model did not answer inside ``AGENT_TIMEOUT_SECONDS``."""


class GateRejected(AgentError):
    """The deterministic policy gate refused a proposal (FR-19).

    Raised only where a caller asked for an action the gate blocks; the crew graph handles a
    blocked gate as a normal terminal state instead, so a refusal is still recorded and auditable.
    """

    def __init__(self, blocking: list[str], detail: str = "") -> None:
        super().__init__(detail or f"policy gate blocked: {', '.join(blocking)}")
        self.blocking = blocking
