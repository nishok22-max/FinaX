"""NumberGuard — post-hoc provenance check on model prose (FR-22).

Structured output stops the model *choosing* a number that matters: the strategist returns an enum,
never a repay amount, so no LLM output is ever an argument to a transaction. This module covers the
remaining, milder risk — that narrative prose *mentions* a figure the backend never produced, and
the reader takes it for live data.

The design point is what happens on failure. The reply is not suppressed and not silently passed:
it is **flagged**, and the console renders a flagged reply in a visibly degraded style. Suppression
would hide the fact that the model went off-piste; silent passing is the exact failure
``frontend/finax.js`` already documents having made once. Marking it is the honest option, and it
keeps the guard's own false positives cheap — a wrongly-flagged reply is still readable.

The matcher is deliberately generous about *rendering*, because the same backed figure legitimately
appears many ways: ``0.0123`` as ``1.23%``, ``2_400_000_000`` USDC units as ``2,400``, an HF of
``1.1432`` as ``1.14``. Being strict about renderings would flag honest prose constantly, and a
guard nobody trusts is a guard nobody reads.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: Digits that carry no claim about the position: list ordinals, bps/percent denominators, the
#: small integers that appear in ordinary English ("one of the two collaterals", "step 3").
_ALLOWED_BARE: frozenset[float] = frozenset(
    {0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 100.0, 1000.0, 10000.0}
)

#: Numbers with separators/decimals, optionally signed. Trailing '%' is handled by the caller
#: expanding candidate renderings rather than by a second pattern.
#:
#: The ``(?<![\w-])`` lookbehind keeps digits that are part of a *name* from being read as a
#: quantity. Without it "EIP-712" yields the claim ``-712`` (the hyphen parsed as a minus), and
#: "Aave V3" / "ERC-20" / "gemini-3.6-flash" flag the same way — so the guard cried wolf on the
#: product's own vocabulary. A guard that flags "EIP-712" teaches operators to ignore the banner,
#: which costs more than the false negative of skipping a figure written flush against a word.
#: ``.`` is in the lookbehind too: without it a blocked leading digit just moves the scan along,
#: so "gemini-3.6-flash" skips the "3" and then reports "6" as a claim.
_NUMBER_RE = re.compile(
    r"(?<![\w.-])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.-])-?\d+(?:\.\d+)?"
)

#: Addresses, tx hashes and other 0x blobs are identifiers, not quantities.
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")

#: ISO timestamps contain many digits that are not claims about the position.
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:]+(?:[+-][\d:]+|Z)?")

_REL_TOL = 5e-3


def extract_numbers(text: str) -> list[float]:
    """Numeric literals in ``text``, with identifiers and timestamps removed first."""
    cleaned = _ISO_RE.sub(" ", _HEX_RE.sub(" ", text))
    out: list[float] = []
    for raw in _NUMBER_RE.findall(cleaned):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:  # pragma: no cover - the pattern only matches parseable literals
            continue
    return out


def _renderings(value: float) -> set[float]:
    """Ways a single backed figure may legitimately appear in prose.

    Covers rounding to 2/4 decimal places, a ratio written as a percentage (and the reverse),
    a bps figure written as a ratio or a percent, and a 6-decimal token amount written in whole
    units — the conversions that actually occur in this product's own copy.
    """
    out = {value, round(value, 2), round(value, 4), float(int(value))}
    out |= {value * 100, round(value * 100, 2), value / 100, round(value / 100, 2)}
    out |= {value / 10_000, round(value / 10_000, 4)}
    if abs(value) >= 1_000_000:  # a raw 6-decimal token amount rendered in whole units
        out |= {value / 10**6, round(value / 10**6, 2)}
    if abs(value) >= 10**16:  # a WAD figure rendered as a plain ratio
        out |= {value / 10**18, round(value / 10**18, 4)}
    return out


def collect_numbers(payload: Any) -> list[float]:
    """Every numeric leaf in an arbitrary JSON-ish structure (tool results, fact sheets)."""
    found: list[float] = []
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, bool):
            continue  # bool is an int subclass; True is not a quantity
        if isinstance(item, (int, float)):
            found.append(float(item))
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return found


def check(
    text: str, allowed: Iterable[float], *, rel_tol: float = _REL_TOL
) -> tuple[bool, list[float]]:
    """Check every figure in ``text`` against ``allowed``.

    Returns ``(ok, unverified)``. ``ok`` is True when every number in the prose matches some
    backed value under one of its accepted renderings, within ``rel_tol``.
    """
    candidates: set[float] = set()
    for value in allowed:
        candidates |= _renderings(float(value))

    unverified: list[float] = []
    for found in extract_numbers(text):
        if found in _ALLOWED_BARE:
            continue
        if any(
            abs(found - c) <= max(rel_tol * max(abs(found), abs(c)), 1e-9) for c in candidates
        ):
            continue
        unverified.append(found)
    return not unverified, unverified


class NumberGuard:
    """Accumulates the figures observed during one turn, then judges that turn's prose.

    Tool results are fed in as they arrive, so the guard's allow-list is exactly what the model
    was actually shown — not what it could in principle have asked for.
    """

    def __init__(self) -> None:
        self._allowed: list[float] = []

    def observe(self, payload: Any) -> None:
        """Record every numeric leaf of a tool result or fact sheet as legitimately available."""
        self._allowed.extend(collect_numbers(payload))

    @property
    def allowed(self) -> list[float]:
        return list(self._allowed)

    def verify(self, text: str) -> tuple[bool, list[float]]:
        return check(text, self._allowed)

    def annotate(self, text: str) -> tuple[str, bool, list[float]]:
        """Return ``(text, flagged, unverified)``, appending a visible banner when flagged.

        The banner is part of the payload rather than a UI-only concern so that a flagged reply
        stays marked wherever it is read back — the stored chat history, the audit trail, a log.
        """
        ok, unverified = self.verify(text)
        if ok:
            return text, False, []
        figures = ", ".join(f"{n:g}" for n in unverified)
        banner = (
            "\n\n⚠ Unverified figures: "
            f"{figures}. These could not be traced to a backend response and must not be "
            "treated as live data — check the console's own metrics instead."
        )
        return text + banner, True, unverified
