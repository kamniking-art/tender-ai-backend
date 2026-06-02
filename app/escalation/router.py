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
    tg_from: dict = cq.get("from") or {}
    tg_user_id: int | None = tg_from.get("id") if isinstance(tg_from.get("id"), int) else None
    tg_note: str | None = f"tg:{tg_user_id}" if tg_user_id is not None else None

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
            await approve_escalation(db, escalation_id, override_note=tg_note)
        else:
            await reject_escalation(db, escalation_id, override_note=tg_note)
    except EscalationStateError as exc:
        logger.warning("Webhook state error for escalation %s: %s", escalation_id, exc)

    # Answer the callback_query to dismiss the spinner on the button (best-effort).
    cq_id: str | None = cq.get("id") if isinstance(cq.get("id"), str) else (
        str(cq["id"]) if cq.get("id") is not None else None
    )
    if cq_id:
        try:
            from app.models import Company as _Company
            from app.telegram_notify.client import TelegramClient
            from app.telegram_notify.service import _extract_telegram_config
            _company = await db.scalar(select(_Company).where(_Company.id == esc.company_id))
            _cfg = _extract_telegram_config(_company.profile or {}) if _company and isinstance(_company.profile, dict) else None
            if _cfg and _cfg.bot_token:
                import httpx as _httpx
                _url = f"https://api.telegram.org/bot{_cfg.bot_token}/answerCallbackQuery"  # noqa: E501 — direct call, no Warsaw proxy needed
                async with _httpx.AsyncClient(timeout=5) as _http:
                    await _http.post(_url, json={"callback_query_id": cq_id})
        except Exception:
            logger.warning("Failed to answer callback_query id=%s", cq_id)

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
