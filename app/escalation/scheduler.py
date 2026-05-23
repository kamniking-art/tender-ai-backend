"""EscalationTimeoutScheduler — auto-timeout stale pending escalations.

Follows the same asyncio-loop pattern as tender_tasks/scheduler.py.
Runs every 60 minutes and marks escalations as 'timeout' if they have been
in an active state (pending/reminded) for longer than ESCALATION_TIMEOUT_HOURS.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.escalation.schema import is_escalation_stale  # re-export for callers  # noqa: F401

logger = logging.getLogger("uvicorn.error")

_CHECK_INTERVAL_SECONDS = 60 * 60  # 1 hour


class EscalationTimeoutScheduler:
    """Periodically marks stale pending/reminded escalations as 'timeout'."""

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
                logger.exception("EscalationTimeoutScheduler iteration failed")
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

    async def _run_once(self) -> None:
        from sqlalchemy import select

        from app.escalation.service import Escalation, timeout_escalation
        from app.escalation.schema import ACTIVE_STATUSES

        timeout_hours = settings.escalation_timeout_hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=timeout_hours)

        async with AsyncSessionLocal() as db:
            stale = list(
                (
                    await db.scalars(
                        select(Escalation).where(
                            Escalation.status.in_(list(ACTIVE_STATUSES)),
                            Escalation.created_at < cutoff,
                        )
                    )
                ).all()
            )

            timed_out = 0
            for esc in stale:
                try:
                    await timeout_escalation(db, esc.escalation_id)
                    timed_out += 1
                except Exception:
                    logger.exception(
                        "Failed to timeout escalation %s", esc.escalation_id
                    )

        if timed_out:
            logger.info(
                "EscalationTimeoutScheduler: %d escalation(s) timed out "
                "(threshold=%dh)",
                timed_out,
                timeout_hours,
            )


scheduler = EscalationTimeoutScheduler()
