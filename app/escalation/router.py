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
    if len(parts) != 2:
        return Response(status_code=status.HTTP_200_OK)

    # ── Clarification callbacks ───────────────────────────────────────────────
    if parts[0] in ("clarif_approve", "clarif_send"):
        return await _handle_clarification_callback(
            db, parts[0], parts[1], cq_id=cq.get("id"), tg_note=tg_note,
        )

    if parts[0] not in ("approve", "reject"):
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
            from app.telegram_notify.client import TelegramClient as _TgClient
            from app.telegram_notify.service import _extract_telegram_config
            _company = await db.scalar(select(_Company).where(_Company.id == esc.company_id))
            _cfg = _extract_telegram_config(_company.profile or {}) if _company and isinstance(_company.profile, dict) else None
            if _cfg and _cfg.bot_token:
                _toast = "Решение принято ✅" if action == "approve" else "Отклонено ❌"
                _tg = _TgClient(timeout_sec=10)
                try:
                    # 1. Dismiss spinner + show toast notification.
                    await _tg.answer_callback_query(
                        bot_token=_cfg.bot_token,
                        callback_query_id=cq_id,
                        text=_toast,
                    )
                    # 2. Remove inline keyboard from the original escalation message.
                    if esc.telegram_message_id:
                        await _tg.edit_message_reply_markup(
                            bot_token=_cfg.bot_token,
                            chat_id=_cfg.chat_id,
                            message_id=int(esc.telegram_message_id),
                        )
                finally:
                    await _tg.close()
        except Exception:
            logger.warning("Failed to answer callback_query id=%s", cq_id, exc_info=True)

    return Response(status_code=status.HTTP_200_OK)


async def _handle_clarification_callback(
    db: AsyncSession,
    action: str,
    question_id_str: str,
    *,
    cq_id: object,
    tg_note: str | None,
) -> Response:
    """Process clarif_approve / clarif_send callbacks from Telegram inline buttons."""
    from app.clarification.service import ClarificationQuestion, approve_question, mark_sent
    from app.clarification.schema import ClarificationStateError as _ClarStateError

    try:
        q_id = UUID(question_id_str)
    except ValueError:
        logger.warning("Webhook: invalid clarification question_id: %s", question_id_str)
        return Response(status_code=status.HTTP_200_OK)

    question = await db.scalar(
        select(ClarificationQuestion).where(ClarificationQuestion.id == q_id)
    )
    if question is None:
        logger.warning("Webhook: clarification question %s not found", q_id)
        return Response(status_code=status.HTTP_200_OK)

    try:
        if action == "clarif_approve":
            await approve_question(db, q_id)
            _toast = "Вопрос одобрен ✅"
        else:
            await mark_sent(db, q_id)
            _toast = "Вопрос отправлен 📤"
    except _ClarStateError as exc:
        logger.warning("Webhook clarification state error for %s: %s", q_id, exc)
        _toast = "Уже обработан"

    # After approve: edit message to replace Одобрить with Отправить (best-effort).
    if action == "clarif_approve" and question.telegram_message_id:
        try:
            from app.models import Company as _CompanyForEdit
            from app.telegram_notify.client import TelegramClient as _TgForEdit
            from app.telegram_notify.service import _extract_telegram_config as _ecfg
            _co2 = await db.scalar(select(_CompanyForEdit).where(_CompanyForEdit.id == question.company_id))
            _cfg2 = _ecfg(_co2.profile or {}) if _co2 and isinstance(_co2.profile, dict) else None
            if _cfg2 and _cfg2.bot_token and _cfg2.chat_id:
                _tg2 = _TgForEdit(timeout_sec=10)
                try:
                    await _tg2.edit_message_reply_markup(
                        bot_token=_cfg2.bot_token,
                        chat_id=_cfg2.chat_id,
                        message_id=int(question.telegram_message_id),
                        reply_markup={"inline_keyboard": [[
                            {"text": "📤 Отправить заказчику", "callback_data": f"clarif_send:{q_id}"},
                        ]]},
                    )
                finally:
                    await _tg2.close()
        except Exception:
            logger.warning("Failed to edit clarification message for q_id=%s", q_id, exc_info=True)

    # Answer callback to dismiss spinner (best-effort).
    if cq_id is not None:
        try:
            from app.models import Company as _Company
            from app.telegram_notify.client import TelegramClient as _TgClient
            from app.telegram_notify.service import _extract_telegram_config
            import httpx as _httpx
            _co = await db.scalar(select(_Company).where(_Company.id == question.company_id))
            _cfg = _extract_telegram_config(_co.profile or {}) if _co and isinstance(_co.profile, dict) else None
            if _cfg and _cfg.bot_token:
                _tg = _TgClient(timeout_sec=5)
                try:
                    await _tg.answer_callback_query(
                        bot_token=_cfg.bot_token,
                        callback_query_id=str(cq_id),
                        text=_toast,
                    )
                finally:
                    await _tg.close()
        except Exception:
            logger.warning("Failed to answer clarification callback id=%s", cq_id, exc_info=True)

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
