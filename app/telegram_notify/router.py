from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Company, User
from app.telegram_notify.client import TelegramClient, TelegramSendError
from app.telegram_notify.service import TelegramConfigError, validate_telegram_config

router = APIRouter(prefix="/telegram", tags=["telegram"])


class TelegramTestRequest(BaseModel):
    text: str


class TelegramTestResponse(BaseModel):
    ok: bool = True


@router.post("/test", response_model=TelegramTestResponse)
async def telegram_test_send(
    payload: TelegramTestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TelegramTestResponse:
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    profile = company.profile if isinstance(company.profile, dict) else {}
    try:
        cfg = validate_telegram_config(profile)
    except TelegramConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "missing_fields": exc.missing_fields},
        ) from exc

    client = TelegramClient(timeout_sec=15)
    try:
        await client.send_message(bot_token=cfg.bot_token, chat_id=cfg.chat_id, text=payload.text)
    except TelegramSendError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    finally:
        await client.close()

    return TelegramTestResponse(ok=True)
