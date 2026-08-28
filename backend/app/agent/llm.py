"""Gemini chat-model factory (HEAVY — never imported at module scope by the app).

One deliberate constraint runs through this file: **only the async entrypoints are used.**
``ChatGoogleGenerativeAI`` is built on ``google-genai``'s async client and supports ``ainvoke`` /
``astream`` natively, so no thread offload is needed — but calling the *sync* ``invoke`` would
block the event loop for the whole round trip, stalling the keeper's own RPC polling behind a
third-party API call. :func:`assert_async_only` exists so that mistake fails a test rather than
showing up as mysterious latency in production.

Every call is wrapped in a timeout by its caller (see :func:`ainvoke_guarded`); a model that
never answers must degrade to a 503, not hold a request open indefinitely.
"""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from typing import Any

from app.agent import _lazy
from app.agent.errors import AgentTimeout

logger = logging.getLogger(__name__)

#: Synchronous Runnable entrypoints. Their async counterparts (``ainvoke``/``abatch``/``astream``)
#: are different attribute names, so matching on the exact name has no false positives.
_SYNC_METHODS = frozenset({"invoke", "batch", "stream"})


def build_chat_model(*, model: str, api_key: str, temperature: float = 0.2) -> Any:
    """Construct the Gemini chat model.

    ``convert_system_message_to_human`` is deliberately not set: Gemini handles system
    instructions natively, and letting the wrapper rewrite them would silently change the prompt
    the safety framing depends on.
    """
    genai = _lazy.google_genai_chat()
    return genai.ChatGoogleGenerativeAI(
        model=model,
        google_api_key=api_key,
        temperature=temperature,
    )


def text_of(content: Any) -> str:
    """Flatten a message's ``content`` to the prose a reader should see.

    ``BaseMessage.content`` is ``str | list[str | dict]``. Gemini 3.x returns the list form —
    ``[{"type": "text", "text": "..."}]`` — where 2.5 returned a bare string. Handling only the
    string case fails two different ways depending on the caller: ``str(content)`` renders a
    Python repr (``[{'type': 'text', ...}]``) straight into the console, while an
    ``isinstance(x, str)`` guard silently discards the model's answer and falls back. Both were
    live defects until Gemini 3.x made the list form the common path.

    Only ``text`` blocks are joined. A ``thinking``/``reasoning`` block is the model's scratchpad,
    not its answer: surfacing it would mislead the reader and would hand :class:`NumberGuard`
    figures the model never actually asserted.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        joined = "\n".join(p for p in parts if p).strip()
        if joined:
            return joined
    return str(content)


async def ainvoke_guarded(runnable: Any, payload: Any, *, timeout_s: float) -> Any:
    """``ainvoke`` with a hard timeout, raising :class:`AgentTimeout` on expiry.

    ``asyncio.wait_for`` cancels the underlying task, so a slow model does not keep consuming a
    connection after the caller has given up on it.
    """
    try:
        return await asyncio.wait_for(runnable.ainvoke(payload), timeout=timeout_s)
    except TimeoutError as exc:
        raise AgentTimeout(f"the model did not answer within {timeout_s:.0f}s") from exc


def assert_async_only(*paths: Path) -> list[str]:
    """Report any synchronous model call site (``.invoke`` / ``.batch`` / ``.stream``).

    A blocking model call inside this event loop would stall the keeper's polling behind a
    third-party API round trip — invisible in review, obvious in production. Worth asserting
    mechanically; see ``tests/test_agent_graph.py``.

    Uses the AST rather than a text search so that prose about these methods (this docstring
    included) is not itself reported.
    """
    offenders: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a syntax error fails elsewhere, loudly
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _SYNC_METHODS:
                offenders.append(f"{path.name}:{node.lineno}: .{func.attr}(...)")
    return offenders
