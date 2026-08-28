"""Metrics + operator controls — observability snapshot and circuit-breaker reset (FR-17, NFR-5)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.models import MetricsSnapshot
from app.core.protection_service import ProtectionService
from app.deps import get_service

router = APIRouter(tags=["metrics"])

Service = Annotated[ProtectionService, Depends(get_service)]


@router.get("/metrics", response_model=MetricsSnapshot)
async def metrics(service: Service) -> MetricsSnapshot:
    return service.metrics()


@router.post("/breaker/reset", response_model=MetricsSnapshot)
async def reset_breaker(service: Service) -> MetricsSnapshot:
    """Operator reset of a tripped circuit breaker (FR-17)."""
    service.reset_breaker()
    return service.metrics()
