"""FastAPI application entrypoint (Phase 0 scaffold).

Boots the service and exposes `/health`. The Control-API vs Background-Worker split, the
autonomous monitoring loop, and the decision pipeline are implemented in Phases 3–5.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Phase 5: start the background worker (async monitor loop) here.
    yield
    # Phase 5: cancel the worker cleanly on shutdown.


app = FastAPI(title="Liquidation Shield Keeper", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    # Best-effort chain probe: never let a dead RPC fail the health endpoint.
    block: int | None = None
    connected = False
    if settings.arbitrum_rpc_url:
        try:
            from app.chain.client import get_client

            client = get_client()
            block = await client.block_number()
            connected = True
        except Exception:  # noqa: BLE001 - health probe is advisory
            connected = False

    return {
        "status": "ok",
        "chain": "arbitrum-one",
        "chain_id": settings.chain_id,
        "rpc_configured": bool(settings.arbitrum_rpc_url),
        "rpc_connected": connected,
        "block_number": block,
        "vault_configured": bool(settings.vault_address),
    }
