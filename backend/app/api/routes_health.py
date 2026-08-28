"""Health endpoint — liveness + a best-effort chain probe."""
from __future__ import annotations

from fastapi import APIRouter

from app.config.settings import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, object]:
    block: int | None = None
    connected = False
    if settings.arbitrum_rpc_url:
        try:
            from app.chain.client import get_client

            block = await get_client().block_number()
            connected = True
        except Exception:  # noqa: BLE001 - health probe is advisory, never fails the endpoint
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
