"""Position endpoints — status, assessment (dry-run), and the bounded protect trigger.

Every mutating call is bounded by the borrower's signed ``RiskParams`` (FR-13): the path borrower
must match the signed ``params.borrower``, and the contract re-verifies the signature on-chain.
Routes are thin — all logic lives in the shared :class:`ProtectionService`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.core.models import AssessmentResponse, ProtectRequest, ProtectResponse
from app.core.protection_service import ProtectionService
from app.deps import get_service

router = APIRouter(prefix="/positions", tags=["positions"])

Service = Annotated[ProtectionService, Depends(get_service)]


def _require_match(borrower: str, body: ProtectRequest) -> None:
    if body.params.borrower.lower() != borrower.lower():
        raise HTTPException(status_code=400, detail="path borrower != signed params.borrower")


@router.get("/{borrower}")
async def get_position(borrower: str, service: Service) -> dict[str, object]:
    registered = service.params_of(borrower)
    trigger = registered[0].hf_trigger_bps if registered else 0
    snap = await service.snapshot(borrower, trigger)
    return {
        "borrower": borrower,
        "state": snap.state.value,
        "hf": snap.hf,
        "collateral_usd": snap.account.collateral_usd,
        "debt_usd": snap.account.debt_usd,
        "has_debt": snap.account.has_debt,
        "registered": registered is not None,
    }


@router.get("/{borrower}/assessment", response_model=AssessmentResponse)
async def get_assessment(borrower: str, service: Service) -> AssessmentResponse:
    registered = service.params_of(borrower)
    if registered is None:
        raise HTTPException(status_code=404, detail="borrower not registered; POST params first")
    return await service.assess(registered[0])


@router.post("/{borrower}/assessment", response_model=AssessmentResponse)
async def post_assessment(borrower: str, body: ProtectRequest, service: Service) -> AssessmentResponse:
    """Register the signed params and return a dry-run assessment (no submission)."""
    _require_match(borrower, body)
    service.register(body.params, body.signature)
    return await service.assess(body.params)


@router.post("/{borrower}/protect", response_model=ProtectResponse)
async def protect(borrower: str, body: ProtectRequest, service: Service) -> ProtectResponse:
    """Manual trigger — assess → simulate → submit, bounded by the signed params (FR-13)."""
    _require_match(borrower, body)
    return await service.protect(body.params, body.signature)
