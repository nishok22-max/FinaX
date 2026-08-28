"""FastAPI application entrypoint (Phase 5).

Control-API vs Background-Worker split: the routers are thin controllers over the shared
:class:`ProtectionService`; the autonomous monitor→decide→submit loop runs in the background
:class:`Worker`, launched by the lifespan on boot and cancelled cleanly on shutdown. The worker
only starts when ``WORKER_ENABLED`` is set and RPC is configured, so importing the app (and unit
tests) never require a chain connection.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import routes_config, routes_health, routes_metrics, routes_positions
from app.config.settings import settings
from app.observability import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    worker = None
    if settings.worker_enabled and settings.arbitrum_rpc_url:
        from app.deps import get_container

        worker = get_container().worker
        worker.start()
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()


from pathlib import Path
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Liquidation Shield Keeper", version="0.5.0", lifespan=lifespan)
app.include_router(routes_health.router)
app.include_router(routes_positions.router)
app.include_router(routes_config.router)
app.include_router(routes_metrics.router)

frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/console", StaticFiles(directory=str(frontend_dir), html=True), name="console")

