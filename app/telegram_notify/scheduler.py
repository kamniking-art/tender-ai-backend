from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import Company
from app.telegram_notify.client import TelegramClient, TelegramSendError
from app.telegram_notify.service import process_company_notifications

logger = logging.getLogger("uvicorn.error")


class TelegramNotifyScheduler:
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
        interval = max(1, settings.telegram_notify_interval_minutes * 60)
        while self._running:
            try:
                await self._run_iteration()
            except Exception:
                logger.exception("telegram notify scheduler iteration failed")
            await asyncio.sleep(interval)

    async def _run_iteration(self) -> None:
        client = TelegramClient(timeout_sec=15)
        try:
            async with AsyncSessionLocal() as db:
                companies = list((await db.scalars(select(Company))).all())
                for company in companies:
                    try:
                        stats = await process_company_notifications(db, company, client)
                        if stats.sent_messages > 0:
                            logger.info(
                                "telegram notify sent: company_id=%s messages=%s items=%s",
                                company.id,
                                stats.sent_messages,
                                stats.sent_items,
                            )
                    except TelegramSendError as exc:
                        logger.warning("telegram notify failed: company_id=%s reason=%s", company.id, str(exc))
                    except Exception:
                        logger.exception("telegram notify company iteration failed: company_id=%s", company.id)
        finally:
            await client.close()


scheduler = TelegramNotifyScheduler()
