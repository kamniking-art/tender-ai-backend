from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.config import settings
from app.models import Company
from app.monitoring.schemas import MonitoringSettings
from app.monitoring.service import run_monitoring_cycle

logger = logging.getLogger("uvicorn.error")


class MonitoringScheduler:
    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_run_by_company: dict[str, float] = {}

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
        while self._running:
            try:
                await self._run_iteration()
            except Exception:
                logger.exception("monitoring scheduler iteration failed")
            await asyncio.sleep(60)

    async def _run_iteration(self) -> None:
        async with AsyncSessionLocal() as db:
            companies = list((await db.scalars(select(Company))).all())
            now_ts = time.time()
            for company in companies:
                profile = company.profile if isinstance(company.profile, dict) else {}
                monitoring = MonitoringSettings.from_profile(profile)
                if not monitoring.enabled:
                    continue
                interval_minutes = monitoring.interval_minutes or settings.monitoring_interval_minutes
                key = str(company.id)
                last = self._last_run_by_company.get(key)
                if last is not None and now_ts - last < max(30, interval_minutes * 60):
                    continue
                self._last_run_by_company[key] = now_ts
                try:
                    result = await run_monitoring_cycle(db, company=company, actor_user_id=None)
                    logger.info(
                        "monitoring run: company_id=%s imported=%s new=%s relevant=%s notifications=%s",
                        company.id,
                        result.imported_total,
                        result.new_tenders,
                        result.relevant_found,
                        result.notifications_sent,
                    )
                except Exception:
                    logger.exception("monitoring run failed: company_id=%s", company.id)


scheduler = MonitoringScheduler()

