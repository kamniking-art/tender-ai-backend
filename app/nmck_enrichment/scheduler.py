from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, func

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.tenders.model import Tender
from app.ai_extraction.text_extract import extract_nmck_from_xlsx

logger = logging.getLogger("uvicorn.error")


class NmckEnrichmentScheduler:
    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        # Observability counters
        self.jobs_started = 0
        self.jobs_completed = 0
        self.jobs_failed = 0
        self.total_enriched = 0
        self.last_run_at: float | None = None
        self.last_run_duration_ms: float | None = None
        self.last_queue_depth: int = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("nmck enrichment scheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def get_stats(self) -> dict:
        return {
            "jobs_started": self.jobs_started,
            "jobs_completed": self.jobs_completed,
            "jobs_failed": self.jobs_failed,
            "total_enriched": self.total_enriched,
            "last_run_at": self.last_run_at,
            "last_run_duration_ms": self.last_run_duration_ms,
            "last_queue_depth": self.last_queue_depth,
        }

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._run_iteration()
            except Exception:
                logger.exception("nmck enrichment scheduler iteration failed")
            await asyncio.sleep(300)  # каждые 5 минут

    async def _run_iteration(self) -> None:
        from app.tender_documents.model import TenderDocument

        self.jobs_started += 1
        t_start = time.monotonic()

        try:
            async with AsyncSessionLocal() as db:
                # Queue depth — сколько тендеров ещё ждут
                queue_result = await db.execute(
                    select(func.count()).select_from(
                        select(Tender.id)
                        .join(TenderDocument, TenderDocument.tender_id == Tender.id)
                        .where(Tender.nmck.is_(None))
                        .where(
                            TenderDocument.file_name.ilike('%.xlsx.zip') |
                            TenderDocument.file_name.ilike('%.xlsx')
                        )
                        .distinct()
                        .subquery()
                    )
                )
                self.last_queue_depth = queue_result.scalar() or 0

                # Берём 20 тендеров без НМЦК у которых есть xlsx документы
                result = await db.execute(
                    select(Tender)
                    .join(TenderDocument, TenderDocument.tender_id == Tender.id)
                    .where(Tender.nmck.is_(None))
                    .where(
                        TenderDocument.file_name.ilike('%.xlsx.zip') |
                        TenderDocument.file_name.ilike('%.xlsx')
                    )
                    .distinct()
                    .limit(20)
                )
                tenders = result.scalars().all()

                enriched = 0
                failed = 0
                for tender in tenders:
                    try:
                        docs_result = await db.execute(
                            select(TenderDocument).where(
                                TenderDocument.tender_id == tender.id
                            )
                        )
                        docs = docs_result.scalars().all()
                        if not docs:
                            continue

                        for doc in docs:
                            fname = (doc.file_name or "").lower()
                            if not (fname.endswith(".xlsx") or fname.endswith(".xlsx.zip")):
                                continue
                            file_path = Path(settings.storage_root) / doc.storage_path
                            if not file_path.exists():
                                continue
                            nmck = extract_nmck_from_xlsx(file_path)
                            if nmck is not None:
                                tender.nmck = nmck
                                tender.nmck_source = "deterministic_enrichment"
                                tender.nmck_confidence = Decimal("0.95")
                                await db.commit()
                                enriched += 1
                                self.total_enriched += 1
                                logger.info(
                                    "nmck enriched: tender_id=%s nmck=%s",
                                    tender.id, nmck,
                                )
                                break
                    except Exception:
                        failed += 1
                        logger.exception("nmck enrichment failed: tender_id=%s", tender.id)

            duration_ms = (time.monotonic() - t_start) * 1000
            self.last_run_at = time.time()
            self.last_run_duration_ms = round(duration_ms, 1)
            self.jobs_completed += 1

            logger.info(
                "nmck enrichment run: enriched=%s failed=%s queue_depth=%s duration_ms=%s total_enriched=%s",
                enriched, failed, self.last_queue_depth, self.last_run_duration_ms, self.total_enriched,
            )

        except Exception:
            self.jobs_failed += 1
            logger.exception("nmck enrichment iteration error")


scheduler = NmckEnrichmentScheduler()
