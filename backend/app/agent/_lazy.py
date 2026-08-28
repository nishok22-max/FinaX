"""Warning-quarantined, cached importer for the optional agent stack.

Two problems this solves, both of which would otherwise hit code far outside this package:

**1. ``filterwarnings = ["error"]``.** ``backend/pyproject.toml`` escalates warnings to errors so
the suite stays clean. A single import-time ``DeprecationWarning`` anywhere in the
langgraph / langchain-core / google-genai tree would therefore abort collection of the *entire*
suite — not fail one test, but prevent 119 unrelated ones from running. Importing inside
``warnings.catch_warnings()`` swaps out the global filter state for the duration of the import, so
the ``"error"`` filter is not in effect while those module bodies execute.

This is stronger than adding ``ignore:`` entries to ``pyproject.toml``, because it needs no
knowledge of *which* warnings the stack emits — and that set changes with every upgrade.
(Measured at the pinned versions — langgraph 1.2.11, langchain-core 1.6.1,
langchain-google-genai 4.3.7 — the import is silent today. This guard is for the upgrade that
isn't silent, which is precisely the one nobody will be watching for.)

**2. The dependency is optional.** Every accessor may raise ``ImportError``; callers use
:func:`agent_stack_available` to degrade cleanly rather than crash.

Nothing here is imported at module scope by any module the app reaches — see
``tests/test_agent_isolation.py``, which fails the day that stops being true.
"""
from __future__ import annotations

import warnings
from typing import Any

# Import once, keep forever: the quarantine only helps on the *first* import of a module (later
# ones are a dict lookup in sys.modules and emit nothing), and these trees are slow to import.
_cache: dict[str, Any] = {}


def _import(path: str) -> Any:
    """Import ``path`` with warnings suppressed, memoising the result."""
    cached = _cache.get(path)
    if cached is not None:
        return cached
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        module = __import__(path, fromlist=["*"])
    _cache[path] = module
    return module


def langgraph_graph() -> Any:
    """``langgraph.graph`` — ``StateGraph``, ``START``, ``END``."""
    return _import("langgraph.graph")


def langgraph_types() -> Any:
    """``langgraph.types`` — ``Command``, ``interrupt``."""
    return _import("langgraph.types")


def lc_messages() -> Any:
    """``langchain_core.messages`` — ``AIMessage``, ``HumanMessage``, ``ToolMessage``, …"""
    return _import("langchain_core.messages")


def lc_tools() -> Any:
    """``langchain_core.tools`` — ``StructuredTool``."""
    return _import("langchain_core.tools")


def google_genai_chat() -> Any:
    """``langchain_google_genai`` — ``ChatGoogleGenerativeAI``."""
    return _import("langchain_google_genai")


def agent_stack_available() -> bool:
    """True when the whole optional stack imports cleanly.

    Never raises: an absent or broken agent dependency must degrade the layer, not the keeper.
    """
    try:
        langgraph_graph()
        langgraph_types()
        lc_messages()
        lc_tools()
        google_genai_chat()
    except Exception:  # noqa: BLE001 - any import failure means "unavailable", not "crash"
        return False
    return True
