"""Isolation guarantees for the optional agent layer (``app/agent``).

The agentic layer is an *optional extra*: the keeper must import, boot, and pass its whole suite
with langgraph / langchain / google-genai absent. Two properties make that true, and both are
easy to break by accident, so both are asserted here:

1. **No heavy import reaches module scope.** ``backend/pyproject.toml`` sets
   ``filterwarnings = ["error"]``; a single import-time ``DeprecationWarning`` from the agent
   stack would abort collection of the *entire* suite. Every third-party agent import therefore
   goes through ``app.agent._lazy``, inside a ``warnings.catch_warnings()`` quarantine, and is
   only reached from inside a function body. ``test_importing_app_does_not_import_langgraph``
   fails the day someone adds a top-level ``import langgraph`` to a module the app reaches.

2. **Disabled means inert.** With ``AGENT_ENABLED`` unset the agent routes refuse cleanly, the
   rest of the API is untouched, and no database file is created anywhere.

These tests deliberately do *not* skip when the agent extra is missing — an absent stack is the
configuration they most need to cover.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.runtime import agent_status, get_runtime, reset_runtime_for_tests
from app.config.settings import settings
from app.main import app

_HEAVY_PREFIXES = ("langgraph", "langchain", "langchain_core", "langchain_google_genai")


#: Source for the fresh-interpreter probe (see the test below). Kept inline rather than in a
#: helper module so what the subprocess actually does is readable right here.
_PROBE_TEMPLATE = (
    "import json, sys\n"
    "import app.main\n"
    "prefixes = {prefixes!r}\n"
    "leaked = sorted(n for n in sys.modules if n.split('.')[0] in prefixes)\n"
    "print(json.dumps(leaked[:20]))\n"
)


def test_importing_app_does_not_import_langgraph() -> None:
    """Importing the app must not drag in the agent stack.

    Guards the collection of every other test in this suite: under ``filterwarnings=["error"]``
    an import-time warning from that dependency tree is a collection error, not a warning — it
    would stop 119 unrelated tests from running at all.

    Runs in a **fresh interpreter** because ``test_agent_graph`` and ``test_agent_chat`` legitimately
    import langgraph earlier in the same session; checking this process's ``sys.modules`` would
    measure test-collection order rather than the app's own imports.
    """
    probe = _PROBE_TEMPLATE.format(prefixes=set(_HEAVY_PREFIXES))
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, check=False,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, f"the probe failed to import app.main:\n{result.stderr}"
    leaked = json.loads(result.stdout.strip().splitlines()[-1])
    assert not leaked, (
        "importing app.main pulled in the optional agent stack: "
        f"{leaked}. Move the import inside a function body and route it through "
        "app.agent._lazy so filterwarnings=['error'] cannot turn its import-time "
        "DeprecationWarnings into a suite-wide collection failure."
    )


@pytest.fixture
def disabled_layer(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Pin the agent layer off, exactly as an unconfigured deployment runs."""
    monkeypatch.setattr(settings, "agent_enabled", False)
    reset_runtime_for_tests(None)
    yield
    reset_runtime_for_tests(None)


def test_runtime_is_none_when_the_flag_is_off(disabled_layer: None) -> None:
    assert get_runtime() is None
    status = agent_status()
    assert status.enabled is False
    assert status.reason == "AGENT_ENABLED is false"


def test_status_names_a_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled layer must say which precondition is unmet, not just 'disabled'."""
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    reset_runtime_for_tests(None)
    try:
        assert agent_status().reason == "GEMINI_API_KEY is not configured"
    finally:
        reset_runtime_for_tests(None)


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/agent/chat", {"message": "why is this position at risk?"}),
        ("post", "/agent/crew/run", {"borrower": "0x" + "11" * 20}),
        ("get", "/agent/proposals", None),
        ("get", "/agent/proposals/1", None),
        ("post", "/agent/proposals/1/approve", {"approved_by": "ops"}),
        ("post", "/agent/proposals/1/reject", {"rejected_by": "ops"}),
        ("get", "/agent/tuning", None),
        ("get", "/agent/audit", None),
        ("post", "/agent/panic", {}),
    ],
)
def test_agent_routes_refuse_cleanly_when_disabled(
    disabled_layer: None, method: str, path: str, body: dict[str, object] | None
) -> None:
    """503 with the actual reason — never a 500, and never a silent success."""
    with TestClient(app) as client:
        call = getattr(client, method)
        response = call(path) if body is None else call(path, json=body)
    assert response.status_code == 503
    assert "AGENT_ENABLED" in response.json()["detail"]


def test_status_is_always_200_even_when_disabled(disabled_layer: None) -> None:
    """The console polls this to decide what to render; an error would look like a broken page."""
    with TestClient(app) as client:
        response = client.get("/agent/status")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["reason"] == "AGENT_ENABLED is false"
    assert body["pending_proposals"] == 0


def test_existing_routes_are_unaffected_by_the_disabled_layer(disabled_layer: None) -> None:
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/agent/does-not-exist").status_code == 404


def test_no_database_file_is_created_when_disabled(disabled_layer: None) -> None:
    """A run without the layer must leave the checkout exactly as it found it."""
    db = Path(settings.agent_db_path)
    existed = db.exists()
    with TestClient(app) as client:
        client.get("/health")
        client.get("/agent/status")
    assert db.exists() == existed, f"{db} was created while the agent layer was disabled"
