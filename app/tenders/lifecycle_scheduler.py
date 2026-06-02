"""TenderLifecycleScheduler — automatic status transitions based on deadlines.

Runs every hour and marks tenders whose submission_deadline has passed as
'expired' when they are still in an undecided status (new / notified / analyzing).

Semantics:
  - 'rejected' = we decided not to participate  (human/AI decision)
  - 'expired'  = deadline passed before a decision was made
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import update

from app.core.database import AsyncSessionLocal
from app.tenders.model import Tender

logger = logging.getLogger("uvicorn.error")

_CHECK_INTERVAL_SECONDS = 60 * 60  # 1 hour

# Statuses that are still "undecided" — eligible for auto-expire.
_UNDECIDED_STATUSES = ("new", "notified", "analyzing")


class TenderLifecycleScheduler:
    """Periodically expires tenders whose submission_deadline has passed."""

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
        while self._running:
            try:
                await self._run_once()
            except Exception:
                logger.exception("TenderLifecycleScheduler iteration failed")
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

    async def _run_once(self) -> None:
        now = datetime.now(UTC)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(Tender)
                .where(
                    Tender.status.in_(_UNDECIDED_STATUSES),
                    Tender.submission_deadline.isnot(None),
                    Tender.submission_deadline < now,
                )
                .values(status="expired")
                .returning(Tender.id)
            )
            expired_ids = result.fetchall()
            if expired_ids:
                await db.commit()
                logger.info(
                    "lifecycle: expired %d tenders (deadline passed)",
                    len(expired_ids),
                )


scheduler = TenderLifecycleScheduler()
