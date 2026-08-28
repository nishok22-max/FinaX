"""The deterministic policy gate (FR-19) — pure, synchronous, and free of any model output.

Every agent-originated proposal passes through :func:`evaluate` before it is persisted, and
through it **again** — against a freshly recomputed assessment — immediately before it executes.
That second pass is the point: a proposal is a statement about a position at a moment, and the
position keeps moving while a human reads it.

Three properties are deliberate:

* **No LLM input reaches this file.** Every argument is a backend-computed model. The gate
  recomputes the conditions it cares about from raw integers rather than trusting a narrated
  claim, so a confused or adversarial model cannot talk its way past a bound.
* **No check short-circuits.** All of them run and all of them are reported, so the console can
  render the complete checklist rather than the first failure. Seeing sixteen green rows is what
  makes the safety architecture legible; seeing which single row is red is what makes a refusal
  actionable.
* **The borrower's signed mandate and the operator's limits both apply, and the stricter wins.**
  ``RiskParams`` is the outer bound the borrower consented to; :class:`PolicyLimits` is the
  keeper's own, tighter one. Neither can widen the other.

The gate is an *additional* constraint, never a replacement: the circuit breaker, the in-flight
lock, the viability model, the simulator and the vault's own on-chain checks all still run
afterwards on the normal execution path.
"""
from __future__ import annotations

from typing import Literal

from app.agent.models import GateCheck, PolicyDecision, PolicyLimits
from app.config.arbitrum import BPS
from app.core.models import AssessmentResponse, MetricsSnapshot, PositionSnapshot, RiskParams
from app.core.state import PositionState

#: Failing either of these means the keeper itself has said "stop" — not that this particular
#: proposal is wrong. They are reported as ``hard_block`` so the console can distinguish
#: "system paused" from "this proposal does not qualify".
_HARD_BLOCK_CHECKS = frozenset({"breaker_ok", "not_inflight"})

#: States a rescue may be proposed from. Excludes READY/SUBMITTED (a rescue is already moving),
#: RESTORED/HEALTHY (nothing to do) and DECLINED (the pipeline just said no).
_ACTIONABLE_STATES = frozenset({PositionState.WATCH, PositionState.ASSESSING})


def repay_value_base(repay_amount: int, *, debt_price_base: int, debt_decimals: int) -> int:
    """Value a debt-token repay amount in Aave's USD base units (1e8).

    Bounds are checked in base currency rather than token units on purpose: ``repay_amount`` is
    denominated in the debt asset's own decimals, so "is this repay too large" is meaningless
    without the price. Comparing 2_400_000_000 against a debt figure would be nonsense for a
    6-decimal token and differently nonsense for an 18-decimal one.
    """
    if debt_decimals < 0 or debt_price_base <= 0:
        return 0
    return int(repay_amount * debt_price_base // 10**debt_decimals)


def evaluate(
    *,
    params: RiskParams | None,
    assessment: AssessmentResponse,
    snapshot: PositionSnapshot,
    metrics: MetricsSnapshot,
    limits: PolicyLimits,
    now: float,
    debt_price_base: int,
    debt_decimals: int,
    recent_proposals_borrower: int = 0,
    recent_proposals_global: int = 0,
) -> PolicyDecision:
    """Run every check and return the full verdict.

    ``params`` is optional so an unregistered borrower produces a clean ``registered`` failure
    instead of an exception; the remaining mandate-dependent checks then report that they could
    not be evaluated rather than silently passing.
    """
    checks: list[GateCheck] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(GateCheck(name=name, passed=passed, detail=detail))

    account = snapshot.account
    # Recomputed from the raw WAD integer, not read off a float or a narrated claim.
    hf_bps = account.health_factor // (10**14)

    # 1. A live, borrower-signed mandate must exist — without it there is nothing to execute.
    add("registered", params is not None,
        "signed mandate on file" if params else "borrower has no registered RiskParams")

    if params is None:
        # Every mandate-derived check is unevaluable. Report them as failed with an explicit
        # reason rather than omitting them, so the checklist stays a fixed shape in the UI.
        for name in ("mandate_not_expired", "cost_within_mandate", "collateral_allowlisted",
                     "target_in_signed_band", "hf_below_trigger"):
            add(name, False, "no mandate to check against")
    else:
        # 2. deadline == 0 means "no expiry", matching the vault's own Expired check.
        add("mandate_not_expired", params.deadline == 0 or params.deadline > now,
            "mandate is current" if params.deadline == 0 or params.deadline > now
            else f"mandate expired at {params.deadline:.0f} (now {now:.0f})")

        # 5. Recomputed here rather than trusted: this is the condition that justifies acting.
        add("hf_below_trigger", hf_bps <= params.hf_trigger_bps,
            f"HF {hf_bps} bps vs trigger {params.hf_trigger_bps} bps")

        # 10. Borrower mandate AND operator ceiling; the stricter of the two governs.
        cost_cap = min(params.max_cost_bps, limits.max_cost_bps_ceiling)
        add("cost_within_mandate", assessment.est_cost_bps <= cost_cap,
            f"est cost {assessment.est_cost_bps} bps vs cap {cost_cap} bps "
            f"(mandate {params.max_cost_bps}, operator {limits.max_cost_bps_ceiling})")

        # 11. Mirrors the vault's CollateralNotAllowed. Both sides are already checksummed by
        #     RiskParams' validator and by the pipeline, so a plain comparison is correct.
        allowed = assessment.collateral_asset in params.allowed_collaterals
        add("collateral_allowlisted", allowed,
            f"{assessment.collateral_asset} "
            f"{'is in' if allowed else 'is NOT in'} the signed allow-list")

        # 12. Mirrors the vault's TargetOutOfBand: the dynamic target must sit inside the band
        #     the borrower signed, or executeProtection reverts on chain.
        target_bps = round(assessment.hf_target * BPS)
        in_band = params.hf_target_base_bps <= target_bps <= params.hf_target_max_bps
        add("target_in_signed_band", in_band,
            f"target {target_bps} bps vs signed band "
            f"[{params.hf_target_base_bps}, {params.hf_target_max_bps}]")

    # 3. Nothing to protect without debt (mirrors the vault's NoDebt).
    add("has_debt", account.has_debt,
        f"debt {account.debt_usd:,.2f} USD" if account.has_debt else "position carries no debt")

    # 4. Never propose over a rescue that is already moving, or one just declined.
    add("state_actionable", snapshot.state in _ACTIONABLE_STATES,
        f"state {snapshot.state.value} "
        f"{'is actionable' if snapshot.state in _ACTIONABLE_STATES else 'is not actionable'}")

    # 6. A target within noise of the current HF is not worth a transaction.
    gap_bps = round((assessment.hf_target - assessment.hf) * BPS)
    add("hf_gap_material", gap_bps >= limits.min_hf_gap_bps,
        f"gap to target {gap_bps} bps vs minimum {limits.min_hf_gap_bps} bps")

    # 7-8. The pipeline's own verdict. The agent may not overrule a decline.
    add("assessment_viable", assessment.viable,
        "viable" if assessment.viable else f"not viable: {assessment.reason or 'no reason given'}")
    add("repay_positive", assessment.repay_amount > 0,
        f"repay {assessment.repay_amount} debt-token units")

    # 9. Size bound, in USD base so token decimals cannot distort the comparison.
    repay_base = repay_value_base(
        assessment.repay_amount, debt_price_base=debt_price_base, debt_decimals=debt_decimals
    )
    max_base = int(account.total_debt_base * limits.max_repay_fraction_of_debt)
    # A zero-debt or unpriceable position fails `has_debt` / `repay_positive` above; here an
    # unvalued repay must not pass by arithmetic accident, so require a positive valuation.
    bounded = 0 < repay_base <= max_base
    add("repay_bounded", bounded,
        f"repay {repay_base / 10**8:,.2f} USD vs ceiling {max_base / 10**8:,.2f} USD "
        f"({limits.max_repay_fraction_of_debt:.0%} of debt)")

    # 13-14. Keeper-level stop conditions.
    add("breaker_ok", not metrics.breaker_paused,
        "breaker closed" if not metrics.breaker_paused
        else f"circuit breaker paused ({metrics.breaker_trip_reason})")
    not_inflight = snapshot.borrower not in metrics.in_flight_borrowers
    add("not_inflight", not_inflight,
        "no rescue in flight" if not_inflight else "a rescue is already in flight")

    # 15-16. Rate limits — a loop that proposes every tick is a cost and an alert-fatigue bug.
    add("rate_limit_borrower",
        recent_proposals_borrower < limits.max_proposals_per_borrower_per_hour,
        f"{recent_proposals_borrower} proposals in the last hour for this borrower "
        f"(max {limits.max_proposals_per_borrower_per_hour})")
    add("rate_limit_global",
        recent_proposals_global < limits.max_proposals_global_per_hour,
        f"{recent_proposals_global} proposals in the last hour overall "
        f"(max {limits.max_proposals_global_per_hour})")

    blocking = [c.name for c in checks if not c.passed]
    severity: Literal["ok", "soft_block", "hard_block"]
    if not blocking:
        severity = "ok"
    elif _HARD_BLOCK_CHECKS.intersection(blocking):
        severity = "hard_block"
    else:
        severity = "soft_block"

    return PolicyDecision(
        allowed=not blocking, checks=checks, blocking=blocking, severity=severity
    )
