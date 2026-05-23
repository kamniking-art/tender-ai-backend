from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.escalation.schema import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    EscalationStateError,
)
from app.escalation.service import (
    Escalation,
    approve_escalation,
    create_escalation,
    get_active_escalation,
    reject_escalation,
)
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/escalations", tags=["escalations"])


# ── Response schema ────────────────────────────────────────────────────────────


class EscalationResponse(BaseModel):
    escalation_id: UUID
    company_id: UUID
    escalation_type: str | None
    reason: str
    confidence: float | None
    status: str
    approved_by: UUID | None
    approved_at: datetime | None
    override_note: str | None
    telegram_message_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ── Telegram webhook (public — no auth, called by Telegram) ───────────────────


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Receive Telegram callback_query updates and process approve/reject actions.

    Idempotent: repeated callbacks on terminal escalations are silently ignored.
    No authentication — endpoint is public (called by Telegram's servers).
    """
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=status.HTTP_200_OK)

    cq = body.get("callback_query") or {}
    callback_data: str = cq.get("data") or ""

    if not callback_data:
        return Response(status_code=status.HTTP_200_OK)

    parts = callback_data.split(":", 1)
    if len(parts) != 2 or parts[0] not in ("approve", "reject"):
        return Response(status_code=status.HTTP_200_OK)

    action, escalation_id_str = parts
    try:
        escalation_id = UUID(escalation_id_str)
    except ValueError:
        logger.warning("Webhook received invalid escalation_id: %s", escalation_id_str)
        return Response(status_code=status.HTTP_200_OK)

    esc = await db.scalar(
        select(Escalation).where(Escalation.escalation_id == escalation_id)
    )
    if esc is None:
        logger.warning("Webhook: escalation %s not found", escalation_id)
        return Response(status_code=status.HTTP_200_OK)

    # Terminal state → no-op (idempotent)
    if esc.status in TERMINAL_STATUSES:
        logger.info(
            "Webhook: escalation %s already terminal (%s), skipping",
            escalation_id,
            esc.status,
        )
        return Response(status_code=status.HTTP_200_OK)

    try:
        if action == "approve":
            await approve_escalation(db, escalation_id)
        else:
            await reject_escalation(db, escalation_id)
    except EscalationStateError as exc:
        logger.warning("Webhook state error for escalation %s: %s", escalation_id, exc)

    return Response(status_code=status.HTTP_200_OK)


# ── Manual approve ─────────────────────────────────────────────────────────────


@router.post("/{escalation_id}/approve", response_model=EscalationResponse)
async def manual_approve(
    escalation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EscalationResponse:
    """Manually approve an escalation (no Telegram required)."""
    esc = await db.scalar(
        select(Escalation).where(
            Escalation.escalation_id == escalation_id,
            Escalation.company_id == current_user.company_id,
        )
    )
    if esc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Escalation not found")

    try:
        esc = await approve_escalation(
            db,
            escalation_id,
            approved_by=current_user.id,
        )
    except EscalationStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return EscalationResponse.model_validate(esc)


# ── Manual reject ──────────────────────────────────────────────────────────────


@router.post("/{escalation_id}/reject", response_model=EscalationResponse)
async def manual_reject(
    escalation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EscalationResponse:
    """Manually reject an escalation (no Telegram required)."""
    esc = await db.scalar(
        select(Escalation).where(
            Escalation.escalation_id == escalation_id,
            Escalation.company_id == current_user.company_id,
        )
    )
    if esc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Escalation not found")

    try:
        esc = await reject_escalation(db, escalation_id)
    except EscalationStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return EscalationResponse.model_validate(esc)


# ── Status query ───────────────────────────────────────────────────────────────


@router.get("/tender/{tender_id}", response_model=EscalationResponse | None)
async def get_escalation_for_tender(
    tender_id: UUID,
    escalation_type: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EscalationResponse | None:
    """Return the active escalation for a tender, or null if none exists."""
    esc = await get_active_escalation(
        db,
        company_id=current_user.company_id,
        escalation_type=escalation_type or "decision_review",
        tender_id=tender_id,
    )
    if esc is None:
        return None
    return EscalationResponse.model_validate(esc)
