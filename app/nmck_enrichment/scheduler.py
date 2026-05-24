from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.tenders.model import Tender
from app.ai_extraction.text_extract import extract_nmck_from_xlsx

logger = logging.getLogger("uvicorn.error")


class NmckEnrichmentScheduler:
    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None

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
                logger.exception("nmck enrichment scheduler iteration failed")
            await asyncio.sleep(300)  # каждые 5 минут

    async def _run_iteration(self) -> None:
        from app.tender_documents.model import TenderDocument

        async with AsyncSessionLocal() as db:
            # Берём тендеры без НМЦК у которых есть документы
            result = await db.execute(
                select(Tender)
                .where(Tender.nmck.is_(None))
                .limit(20)
            )
            tenders = result.scalars().all()

            enriched = 0
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
                            logger.info(
                                "nmck enriched: tender_id=%s nmck=%s",
                                tender.id, nmck,
                            )
                            break
                except Exception:
                    logger.exception("nmck enrichment failed: tender_id=%s", tender.id)

            if enriched:
                logger.info("nmck enrichment run: enriched=%s", enriched)


scheduler = NmckEnrichmentScheduler()
