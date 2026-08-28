"""Background worker — the autonomous monitor→decide→submit loop (FR-1, FR-12).

A cancellation-friendly asyncio loop that calls ``ProtectionService.tick`` every poll interval.
(An APScheduler ``AsyncIOScheduler`` would wrap the same coroutine; a bare loop keeps the worker
dependency-free and trivially testable, and honors the same "idempotent async decision loop"
contract from the architecture.) The FastAPI lifespan starts it on boot and cancels it cleanly on
shutdown.
"""
from __future__ import annotations

import asyncio
import logging

from app.core.protection_service import ProtectionService

logger = logging.getLogger(__name__)


class Worker:
    """Runs ``service.tick()`` on a fixed interval until cancelled."""

    def __init__(self, service: ProtectionService, *, interval_seconds: float) -> None:
        self._service = service
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="protection-worker")
            logger.info("worker started (interval=%.1fs)", self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("worker stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._service.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker tick raised")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass  # normal: interval elapsed, run the next tick
