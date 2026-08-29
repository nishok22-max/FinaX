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

from app.api import routes_agent, routes_config, routes_health, routes_metrics, routes_positions
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
    if settings.agent_enabled:
        # Open the agent's store up front so the first request does not pay for it. Guarded by
        # the flag (default off) so a run without the layer creates no database file at all.
        from app.agent.runtime import get_runtime

        agent_runtime = get_runtime()
        if agent_runtime is not None:
            await agent_runtime.store.connect()
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()
        # Release the RPC transports so the process exits without leaking aiohttp sessions.
        from app.agent.runtime import close_runtime
        from app.chain.client import close_client
        from app.deps import close_container

        await close_runtime()  # no-op when the agent layer never started
        await close_container()
        await close_client()


from pathlib import Path
from typing import Any

from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Liquidation Shield Keeper", version="0.5.0", lifespan=lifespan)
app.include_router(routes_health.router)
app.include_router(routes_positions.router)
app.include_router(routes_config.router)
app.include_router(routes_metrics.router)
# Mounted unconditionally; every route self-gates to a 503 naming the unmet precondition, so the
# API surface is stable whether or not the optional agent extra is installed.
app.include_router(routes_agent.router)

class _RevalidatingStatics(StaticFiles):
    """Serve the console with ``Cache-Control: no-cache``.

    ``StaticFiles`` sends an ``ETag`` and ``Last-Modified`` but no ``Cache-Control``, so browsers
    fall back to heuristic caching and will serve ``finax.js`` from cache **without revalidating**.
    The failure mode is nasty precisely because it looks like a code bug: an edit lands on disk,
    the server returns the new file to ``curl``, and the page keeps running the old one. That cost
    a real debugging cycle here - a fixed status comparison appeared not to take effect.

    ``no-cache`` does not mean "do not store"; it means "revalidate before use", so the ETag still
    does its job and unchanged files still come back as a cheap 304.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
if frontend_dir.exists():
    app.mount(
        "/console", _RevalidatingStatics(directory=str(frontend_dir), html=True), name="console"
    )

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/console/")


