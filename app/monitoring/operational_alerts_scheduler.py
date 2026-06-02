"""OperationalAlertsScheduler — periodic operational health checks.

Runs every N minutes (default 30), queries v_queue_backlog per company and
sends a Telegram warning to companies whose Telegram is configured when:
  - overdue_tasks  > 0
  - running_actions stuck beyond a threshold (future)

Follows the same asyncio-loop pattern as other schedulers in this project.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal

logger = logging.getLogger("uvicorn.error")

_DEFAULT_INTERVAL_MINUTES = 30


class OperationalAlertsScheduler:
    """Periodically checks operational metrics and fires Telegram alerts."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        interval = max(60, getattr(settings, "operational_alerts_interval_minutes", _DEFAULT_INTERVAL_MINUTES) * 60)
        while self._running:
            try:
                await self._run_once()
            except Exception:
                logger.exception("OperationalAlertsScheduler iteration failed")
            await asyncio.sleep(interval)

    async def _run_once(self) -> None:
        async with AsyncSessionLocal() as db:
            await _check_queue_backlog(db)


async def _check_queue_backlog(db: AsyncSession) -> None:
    """Query v_queue_backlog and log/alert on overdue tasks per company."""
    try:
        result = await db.execute(text("SELECT * FROM v_queue_backlog"))
        keys = list(result.keys())
        rows = [dict(zip(keys, row)) for row in result.fetchall()]
    except Exception:
        logger.warning("OperationalAlertsScheduler: v_queue_backlog unavailable", exc_info=True)
        return

    for row in rows:
        overdue = row.get("overdue_tasks") or 0
        company_name = row.get("company_name") or row.get("company_id", "unknown")

        if overdue > 0:
            logger.warning(
                "operational_alert: overdue_tasks company=%s overdue=%d pending=%d",
                company_name,
                overdue,
                row.get("pending_tasks") or 0,
            )
            await _notify_company(db, row, overdue)


async def _notify_company(db: AsyncSession, row: dict, overdue: int) -> None:
    """Send a Telegram warning to the company if Telegram is configured."""
    company_id = row.get("company_id")
    if not company_id:
        return

    try:
        from sqlalchemy import select as _select
        from app.models import Company
        from app.telegram_notify.client import TelegramClient, TelegramSendError
        from app.telegram_notify.service import _extract_telegram_config

        company = await db.scalar(_select(Company).where(Company.id == company_id))
        if company is None or not isinstance(company.profile, dict):
            return

        cfg = _extract_telegram_config(company.profile)
        if not cfg or not cfg.enabled or not cfg.bot_token or not cfg.chat_id:
            return

        pending = row.get("pending_tasks") or 0
        text_msg = (
            f"⚠️ Операционный алерт\n\n"
            f"Просроченных задач: {overdue}\n"
            f"Задач в очереди: {pending}\n\n"
            f"Проверьте панель мониторинга."
        )

        client = TelegramClient(timeout_sec=settings.warsaw_timeout_sec)
        try:
            await client.send_message(
                bot_token=cfg.bot_token,
                chat_id=cfg.chat_id,
                text=text_msg,
            )
            logger.info(
                "operational_alert sent: company=%s overdue=%d",
                row.get("company_name") or company_id,
                overdue,
            )
        except TelegramSendError as exc:
            logger.warning(
                "operational_alert telegram failed: company=%s reason=%s",
                company_id,
                str(exc),
            )
        finally:
            await client.close()

    except Exception:
        logger.exception("operational_alert notify failed: company_id=%s", company_id)


scheduler = OperationalAlertsScheduler()
