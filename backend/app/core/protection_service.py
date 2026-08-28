"""Shared Protection Service — the one pipeline the API and the worker both call.

The API routes do **not** run the decision pipeline inline; they call this service, and so does the
autonomous worker. It owns the registry of borrower-signed positions, the per-borrower state map,
the in-flight lock (FR-16), the circuit breaker (FR-17), and the counters (NFR-5), and it sequences
the full autonomous tick: ``breaker → in-flight → monitor → risk → decision → sizing → viability →
simulate/bump → submit → await receipt → update state/breaker``.

``simulator``/``submitter`` are optional: with neither, the service is assessment-only (safe default
when no keeper key is configured); with a simulator but no submitter it dry-runs only.
"""
from __future__ import annotations

import logging

from app import observability as obs
from app.config.settings import settings
from app.core.breaker import CircuitBreaker
from app.core.inflight import InFlightRegistry
from app.core.models import (
    AssessmentResponse,
    MetricsSnapshot,
    PositionSnapshot,
    ProtectResponse,
    RiskParams,
)
from app.core.monitor import PositionMonitor
from app.core.pipeline import AssessmentPipeline
from app.core.simulator import Simulator
from app.core.state import PositionState
from app.core.submitter import Submitter

logger = logging.getLogger(__name__)

# Shown when a rescue restored HF via the submitter's direct-repay fallback rather
# than the vault's atomic flash-loan path, so the UI never implies the vault ran.
_FALLBACK_REASON = (
    "HF restored via direct Aave repay - the atomic vault path was unavailable "
    "on this fork (executeProtection did not execute)"
)


class ProtectionService:
    """Registry + orchestration shared by the control API and the background worker."""

    def __init__(
        self,
        pipeline: AssessmentPipeline,
        monitor: PositionMonitor,
        *,
        inflight: InFlightRegistry,
        breaker: CircuitBreaker,
        counters: obs.Counters,
        simulator: Simulator | None = None,
        submitter: Submitter | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._monitor = monitor
        self._inflight = inflight
        self._breaker = breaker
        self._counters = counters
        self._simulator = simulator
        self._submitter = submitter
        self._registry: dict[str, tuple[RiskParams, str]] = {}
        self._states: dict[str, PositionState] = {}
        self.autonomous_enabled = True

    # --- Registry --------------------------------------------------------------------------

    def register(self, params: RiskParams, signature: str) -> None:
        """Store a borrower's signed params so the worker can protect them autonomously."""
        self._registry[params.borrower] = (params, signature)
        self._states.setdefault(params.borrower, PositionState.HEALTHY)

    def unregister(self, borrower: str) -> None:
        self._registry.pop(borrower, None)
        self._states.pop(borrower, None)

    def registered(self) -> list[str]:
        return list(self._registry)

    def state_of(self, borrower: str) -> PositionState:
        return self._states.get(borrower, PositionState.HEALTHY)

    def params_of(self, borrower: str) -> tuple[RiskParams, str] | None:
        return self._registry.get(borrower)

    # --- Read-only assessment / monitoring -------------------------------------------------

    async def assess(self, params: RiskParams, *, sigma: float | None = None) -> AssessmentResponse:
        return await self._pipeline.assess(params, sigma=sigma)

    async def snapshot(self, borrower: str, hf_trigger_bps: int = 0) -> PositionSnapshot:
        """Poll one borrower's account and refresh its state (unless a rescue is in flight)."""
        snap = await self._monitor.poll_once(borrower, hf_trigger_bps)
        if not self._inflight.is_locked(borrower):
            self._states[borrower] = snap.state
        return snap

    # --- The protective action (manual POST /protect or autonomous tick) -------------------

    async def protect(self, params: RiskParams, signature: str) -> ProtectResponse:
        """Assess → simulate → submit for one borrower, honoring breaker + in-flight lock."""
        borrower = params.borrower
        self.register(params, signature)

        if not self._breaker.allow():
            self._counters.inc(obs.BREAKER_BLOCKED)
            return ProtectResponse(
                borrower=borrower, state=self.state_of(borrower), submitted=False,
                reason=f"circuit breaker paused ({self._breaker.trip_reason})",
            )
        if not self._inflight.can_start(borrower):
            self._counters.inc(obs.DOUBLE_SUBMIT_BLOCKED)
            return ProtectResponse(
                borrower=borrower, state=self.state_of(borrower), submitted=False,
                reason="rescue already in flight / cooling down",
            )

        self._states[borrower] = PositionState.ASSESSING
        sigma = self._monitor.sigma_for(params.allowed_collaterals[0]) if params.allowed_collaterals else 0.0
        response, plan = await self._pipeline.evaluate(params, sigma=sigma)
        self._counters.inc(obs.ASSESSED)

        if plan is None or not response.viable:
            self._states[borrower] = PositionState.DECLINED
            self._counters.inc(obs.DECLINED)
            return ProtectResponse(
                borrower=borrower, state=PositionState.DECLINED, submitted=False,
                assessment=response, reason=response.reason,
            )

        # Dry-run (FR-8 pre-flight).
        if self._simulator is None:
            self._states[borrower] = PositionState.READY
            return ProtectResponse(
                borrower=borrower, state=PositionState.READY, submitted=False,
                assessment=response, reason="assessment-only (no simulator configured)",
            )
            
        # Demo convenience: on a local fork, stand in for the borrower's wallet and
        # sign the EIP-712 digest server-side when the request carries no signature.
        # Gated behind DEMO_MODE so it can never run in a real deployment - without
        # the gate the UI's "the vault refused it, key never touches the backend"
        # claim is false for exactly the accounts a demo uses.
        if settings.demo_mode:
            clean_sig = signature.removeprefix("0x")
            if not clean_sig or clean_sig == "00" * (len(clean_sig) // 2):
                signature = await self._resolve_demo_signature(borrower, params, signature)

        sim = await self._simulator.simulate(plan, params, signature)
        if not sim.success:
            self._states[borrower] = PositionState.DECLINED
            self._counters.inc(obs.SIMULATED_FAIL)
            return ProtectResponse(
                borrower=borrower, state=PositionState.DECLINED, submitted=False,
                assessment=response, reason=f"simulation reverted: {sim.revert_reason}",
            )
        self._counters.inc(obs.SIMULATED_OK)

        if self._submitter is None:
            self._states[borrower] = PositionState.READY
            return ProtectResponse(
                borrower=borrower, state=PositionState.READY, submitted=False,
                assessment=response, reason="dry-run only (no submitter configured)",
            )

        # Take the in-flight lock right before submitting (FR-16); a racing tick loses here.
        if not self._inflight.acquire(borrower):
            self._counters.inc(obs.DOUBLE_SUBMIT_BLOCKED)
            return ProtectResponse(
                borrower=borrower, state=self.state_of(borrower), submitted=False,
                assessment=response, reason="rescue already in flight (race)",
            )
        try:
            self._states[borrower] = PositionState.SUBMITTED
            self._counters.inc(obs.SUBMITTED)
            sub = await self._submitter.submit(
                plan, params, signature,
                repay_amount=sim.repay_amount, amount_in_maximum=sim.amount_in_maximum,
            )
            if sub.status == 1:
                self._counters.inc(obs.RESTORED)
                self._breaker.record_success()
                self._states[borrower] = PositionState.RESTORED
            else:
                self._counters.inc(obs.REVERTED)
                self._breaker.record_failure()
                self._states[borrower] = PositionState.REVERTED
                if self._breaker.paused:
                    self._counters.inc(obs.BREAKER_TRIPPED)
        except Exception as exc:
            # Previously an exception here escaped the try/finally, so the breaker
            # streak stayed at 0 and the borrower was left pinned in SUBMITTED: a
            # permanently broken submitter would retry forever and FR-17 would
            # never engage. Record it exactly as a reverted receipt is recorded.
            self._counters.inc(obs.REVERTED)
            self._counters.inc(obs.ERRORS)
            self._breaker.record_failure()
            self._states[borrower] = PositionState.REVERTED
            if self._breaker.paused:
                self._counters.inc(obs.BREAKER_TRIPPED)
            logger.exception("submitter raised for borrower=%s", borrower)
            return ProtectResponse(
                borrower=borrower, state=PositionState.REVERTED, submitted=False,
                assessment=response, reason=f"submission failed: {exc}",
            )
        finally:
            self._inflight.release(borrower)

        return ProtectResponse(
            borrower=borrower, state=self._states[borrower],
            submitted=sub.status == 1, tx_hash=sub.tx_hash, assessment=response,
            via_fallback=sub.via_fallback,
            reason=(
                _FALLBACK_REASON if sub.status == 1 and sub.via_fallback
                else None if sub.status == 1
                else "transaction reverted (position unchanged)"
            ),
        )

    # --- Autonomous worker tick ------------------------------------------------------------

    async def tick(self) -> None:
        """One autonomous pass over all registered borrowers (FR-1, FR-12)."""
        if not self.autonomous_enabled:
            return
        for borrower, (params, signature) in list(self._registry.items()):
            try:
                snapshot = await self._monitor.poll_once(borrower, params.hf_trigger_bps)
                if params.allowed_collaterals:
                    await self._monitor.sample_price(params.allowed_collaterals[0])
                if not self._inflight.is_locked(borrower):
                    self._states[borrower] = snapshot.state
                if (
                    snapshot.state == PositionState.ASSESSING
                    and self._breaker.allow()
                    and self._inflight.can_start(borrower)
                ):
                    await self.protect(params, signature)
            except Exception:
                self._counters.inc(obs.ERRORS)
                logger.exception("tick failed for borrower=%s", borrower)

    # --- Config & metrics ------------------------------------------------------------------

    def metrics(self) -> MetricsSnapshot:
        return MetricsSnapshot(
            breaker_paused=self._breaker.paused,
            breaker_consecutive_failures=self._breaker.consecutive_failures,
            breaker_trip_reason=self._breaker.trip_reason,
            in_flight_borrowers=self._inflight.locked_borrowers(),
            registered_positions=len(self._registry),
            counters=self._counters.snapshot(),
            states={b: s.value for b, s in self._states.items()},
        )

    def reset_breaker(self) -> None:
        self._breaker.reset()

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def inflight(self) -> InFlightRegistry:
        return self._inflight

    @property
    def monitor(self) -> PositionMonitor:
        """The monitor whose volatility window ``tick`` fills — the pipeline holds its own.

        Read-only accessor for callers that need the σ actually accumulated by the polling loop
        (the risk signal is computed per assessment and otherwise discarded).
        """
        return self._monitor

    @property
    def simulator(self) -> Simulator | None:
        return self._simulator

    async def _resolve_demo_signature(self, borrower: str, params: RiskParams, raw_signature: str) -> str:
        try:
            from eth_account import Account
            from eth_utils.address import to_checksum_address
            from web3 import AsyncWeb3

            from app.config.arbitrum import vault_abi
            from app.config.settings import settings
            
            Account.enable_unaudited_hdwallet_features()
            mnemonic = "test test test test test test test test test test test junk"
            for idx in range(10):
                acct = Account.from_mnemonic(mnemonic, account_path=f"m/44'/60'/0'/0/{idx}")
                if acct.address.lower() == borrower.lower():
                    vault_addr = settings.vault_address
                    if not vault_addr or self._simulator is None:
                        return raw_signature
                    
                    async def _get_digest(w3: AsyncWeb3) -> bytes:
                        vault = w3.eth.contract(address=to_checksum_address(vault_addr), abi=vault_abi())
                        return await vault.functions.hashRiskParams(params.to_solidity_tuple()).call()
                    
                    digest = await self._simulator._c.call(_get_digest)
                    sig = Account.unsafe_sign_hash(digest, acct.key).signature.hex()
                    return "0x" + sig.removeprefix("0x")
        except Exception as exc:
            logger.warning("could not resolve demo signature: %s", exc)
        return raw_signature

