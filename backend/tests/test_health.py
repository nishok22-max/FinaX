"""Test FastAPI /health route."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.asyncio


async def test_health_endpoint() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["chain"] == "arbitrum-one"
        assert data["chain_id"] == 42161
        assert data["rpc_configured"] is True
        assert data["rpc_connected"] is True
        assert data["block_number"] is not None and data["block_number"] > 0
