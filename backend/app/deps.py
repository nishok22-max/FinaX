"""Composition root — builds the ProtectionService + Worker from settings, wired for the API.

Lazily constructs a single process-wide container so importing the app never requires RPC/keys
(matters for unit tests). ``simulator``/``submitter`` are only wired when a vault address and keeper
key are configured; otherwise the service runs assessment-only, which is the safe default.
"""
from __future__ import annotations

from dataclasses import dataclass

from app import observability as obs
from app.chain.aave import AaveClient
from app.chain.client import ChainClient
from app.chain.erc20 import ERC20Client
from app.chain.oracle import OracleClient
from app.chain.uniswap import UniswapClient
from app.config.settings import settings
from app.core.breaker import CircuitBreaker
from app.core.inflight import InFlightRegistry
from app.core.models import KeeperConfig
from app.core.monitor import PositionMonitor
from app.core.pipeline import AssessmentPipeline
from app.core.protection_service import ProtectionService
from app.core.simulator import Simulator
from app.core.submitter import Submitter
from app.scheduler import Worker


@dataclass
class ServiceContainer:
    service: ProtectionService
    worker: Worker
    config: KeeperConfig
    client: ChainClient


def build_container() -> ServiceContainer:
    client = ChainClient()
    aave = AaveClient(client)
    uniswap = UniswapClient(client)
    oracle = OracleClient(client)
    erc20 = ERC20Client(client)

    pipeline = AssessmentPipeline(aave, uniswap, oracle, erc20, vault_address=settings.vault_address)
    monitor = PositionMonitor(aave, oracle)
    inflight = InFlightRegistry(cooldown_seconds=settings.inflight_cooldown_seconds)
    breaker = CircuitBreaker(settings.breaker_max_consecutive_failures)
    counters = obs.Counters()

    simulator: Simulator | None = None
    submitter: Submitter | None = None
    if settings.vault_address and settings.keeper_private_key:
        submitter = Submitter(
            client, aave, vault_address=settings.vault_address,
            keeper_private_key=settings.keeper_private_key,
        )
        simulator = Simulator(
            client, uniswap, vault_address=settings.vault_address,
            keeper_address=submitter.keeper_address, max_bumps=settings.max_simulation_bumps,
        )

    service = ProtectionService(
        pipeline, monitor, inflight=inflight, breaker=breaker, counters=counters,
        simulator=simulator, submitter=submitter,
    )
    service.autonomous_enabled = settings.autonomous_enabled
    worker = Worker(service, interval_seconds=settings.poll_interval_seconds)
    config = KeeperConfig(
        poll_interval_seconds=settings.poll_interval_seconds,
        breaker_max_consecutive_failures=settings.breaker_max_consecutive_failures,
        inflight_cooldown_seconds=settings.inflight_cooldown_seconds,
        max_simulation_bumps=settings.max_simulation_bumps,
        autonomous_enabled=settings.autonomous_enabled,
    )
    return ServiceContainer(service=service, worker=worker, config=config, client=client)


_container: ServiceContainer | None = None


def get_container() -> ServiceContainer:
    global _container
    if _container is None:
        _container = build_container()
    return _container


def get_service() -> ProtectionService:
    return get_container().service


async def close_container() -> None:
    """Release the container's RPC sessions and drop it (app shutdown)."""
    global _container
    if _container is not None:
        await _container.client.close()
        _container = None


def apply_config(new: KeeperConfig) -> KeeperConfig:
    """Apply a config update to the live service; returns the effective config."""
    c = get_container()
    c.service.autonomous_enabled = new.autonomous_enabled
    c.service.breaker.max_consecutive_failures = new.breaker_max_consecutive_failures
    c.service.inflight.cooldown_seconds = new.inflight_cooldown_seconds
    if c.service.simulator is not None:
        c.service.simulator.max_bumps = new.max_simulation_bumps
    # poll_interval_seconds is applied on the next worker (re)start.
    c.config = new
    return c.config
