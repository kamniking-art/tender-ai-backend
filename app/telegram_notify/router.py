from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Company, User
from app.telegram_notify.client import TelegramClient, TelegramSendError
from app.telegram_notify.service import TelegramConfigError, validate_telegram_config

router = APIRouter(prefix="/telegram", tags=["telegram"])


class TelegramTestRequest(BaseModel):
    text: str


class TelegramTestResponse(BaseModel):
    ok: bool
    error: str | None = None


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
        test_text = (
            f"{payload.text}\n\n"
            f"company_id={company.id}\n"
            f"timestamp={datetime.utcnow().isoformat()}Z\n"
            f"environment={settings.app_version}"
        )
        await client.send_message(bot_token=cfg.bot_token, chat_id=cfg.chat_id, text=test_text)
    except TelegramSendError as exc:
        return TelegramTestResponse(ok=False, error=str(exc))
    finally:
        await client.close()

    return TelegramTestResponse(ok=True, error=None)
