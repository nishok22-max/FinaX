"""Agent runtime — the lazy singleton, its enabled/disabled state, and its lifecycle.

Deliberately import-light: nothing here pulls in the optional agent stack at module scope, so
``app.main`` can import the agent routes unconditionally without any of it being present. The
heavy work (:mod:`app.agent.graph`, :mod:`app.agent.chat`) is imported inside method bodies.

The enabled/disabled decision lives in :func:`agent_status` rather than in ``Settings`` on
purpose: "off" has several distinct causes — the flag, a missing key, an uninstalled extra — and
an operator who is told *which* one is a few seconds from a fix, whereas one told "disabled" is
not. The routes surface that reason verbatim.

Mirrors the ``deps._container`` seam: :func:`reset_runtime_for_tests` is the injection point, so
tests substitute a runtime the same way they already substitute the service container.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agent.models import PolicyLimits
from app.agent.store import AgentStore
from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentStatus:
    """Why the layer is (or is not) available. Always safe to compute; never raises."""

    enabled: bool
    reason: str | None
    model: str | None
    stack_available: bool
    store_ready: bool


def policy_limits() -> PolicyLimits:
    """Operator ceilings from settings, in the shape :mod:`app.agent.policy` expects."""
    return PolicyLimits(
        max_repay_fraction_of_debt=settings.agent_max_repay_fraction,
        max_cost_bps_ceiling=settings.agent_max_cost_bps,
        min_hf_gap_bps=settings.agent_min_hf_gap_bps,
        max_proposals_per_borrower_per_hour=settings.agent_max_proposals_per_hour,
        max_proposals_global_per_hour=settings.agent_max_proposals_global_per_hour,
        proposal_ttl_seconds=settings.agent_proposal_ttl_seconds,
    )


class AgentRuntime:
    """Holds the store and the model configuration for the life of the process."""

    def __init__(self, *, store: AgentStore, model: str, api_key: str, timeout_s: float) -> None:
        self._store = store
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s

    @property
    def store(self) -> AgentStore:
        return self._store

    @property
    def model(self) -> str:
        return self._model

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_s

    @property
    def api_key(self) -> str:
        return self._api_key

    def limits(self) -> PolicyLimits:
        return policy_limits()

    async def aclose(self) -> None:
        await self._store.close()


_runtime: AgentRuntime | None = None
_probed = False


def agent_status() -> AgentStatus:
    """Evaluate availability, naming the first unmet precondition.

    Checks the cheap conditions before the expensive one: the import probe costs a second or two
    on a cold start, and there is no point paying it to tell an operator that the feature flag is
    off. Never raises — a broken agent dependency must degrade this layer, not the keeper.
    """
    store_ready = _runtime is not None and _runtime.store.ready

    if not settings.agent_enabled:
        return AgentStatus(False, "AGENT_ENABLED is false", None, False, store_ready)
    if not settings.gemini_api_key:
        return AgentStatus(False, "GEMINI_API_KEY is not configured", None, False, store_ready)

    from app.agent._lazy import agent_stack_available

    if not agent_stack_available():
        return AgentStatus(
            False,
            'the optional agent extra is not installed (pip install -e ".[agent]")',
            None, False, store_ready,
        )
    return AgentStatus(True, None, settings.agent_model, True, store_ready)


def get_runtime() -> AgentRuntime | None:
    """The process-wide runtime, or ``None`` when the layer is unavailable.

    The probe runs once. A cold import of langgraph is slow enough that repeating it per request
    would be a visible latency bug on the disabled path — which is the path a misconfigured
    deployment spends all its time on.
    """
    global _runtime, _probed
    if _runtime is None and not _probed:
        _probed = True
        status = agent_status()
        if status.enabled:
            _runtime = AgentRuntime(
                store=AgentStore(settings.agent_db_path),
                model=settings.agent_model,
                api_key=settings.gemini_api_key,
                timeout_s=settings.agent_timeout_seconds,
            )
            logger.info("agent layer enabled (model=%s)", settings.agent_model)
        else:
            logger.info("agent layer disabled: %s", status.reason)
    return _runtime


def reset_runtime_for_tests(runtime: AgentRuntime | None) -> None:
    """Inject (or clear) the runtime, mirroring the ``deps._container`` seam.

    Setting ``_probed`` unconditionally means passing ``None`` pins the layer *off* rather than
    merely clearing the cache — otherwise the next call would re-probe and silently re-enable it
    from ambient settings, which is the opposite of what a test asking for "disabled" wants.
    """
    global _runtime, _probed
    _runtime = runtime
    _probed = True


async def close_runtime() -> None:
    """Release the store on shutdown. A no-op when the layer never started."""
    global _runtime, _probed
    if _runtime is not None:
        await _runtime.aclose()
    _runtime = None
    _probed = False
