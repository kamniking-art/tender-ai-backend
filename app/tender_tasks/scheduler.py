import asyncio
import logging

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.tender_tasks.service import mark_overdue_tasks

logger = logging.getLogger("uvicorn.error")


class TaskOverdueScheduler:
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
        interval = max(1, settings.task_sla_check_interval_minutes * 60)
        while self._running:
            try:
                async with AsyncSessionLocal() as db:
                    overdue = await mark_overdue_tasks(db)
                for task_id, tender_id in overdue:
                    logger.info("Task %s for tender %s is overdue.", task_id, tender_id)
            except Exception:
                logger.exception("Task overdue scheduler iteration failed")
            await asyncio.sleep(interval)


scheduler = TaskOverdueScheduler()
