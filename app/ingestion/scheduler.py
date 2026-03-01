import asyncio
import copy
import logging
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.ingestion.eis_opendata.schemas import EISOpenDataSettings
from app.ingestion.eis_opendata.service import run_eis_opendata_ingestion
from app.ingestion.eis_public.client import EISPublicMaintenanceError
from app.ingestion.eis_public.service import run_eis_public_ingestion
from app.models import Company

logger = logging.getLogger("uvicorn.error")


class IngestionScheduler:
    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_run_by_source_company: dict[tuple[str, UUID], float] = {}

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
                logger.exception("ingestion scheduler iteration failed")
            await asyncio.sleep(60)

    async def _run_iteration(self) -> None:
        async with AsyncSessionLocal() as db:
            companies = list((await db.scalars(select(Company))).all())
            now_ts = time.time()

            for company in companies:
                await self._run_eis_public_if_due(db=db, company=company, now_ts=now_ts)
                await self._run_eis_opendata_if_due(db=db, company=company, now_ts=now_ts)

    async def _run_eis_public_if_due(self, db, company: Company, now_ts: float) -> None:
        settings = copy.deepcopy(company.ingestion_settings or {})
        cfg = copy.deepcopy(settings.get("eis_public") or {})
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return

        cooldown_until = _parse_dt(((cfg.get("state") or {}).get("cooldown_until") if isinstance(cfg.get("state"), dict) else None))
        now_utc = datetime.now(UTC)
        if cooldown_until and cooldown_until > now_utc:
            logger.info("eis_public cooldown active: company_id=%s until=%s", company.id, cooldown_until.isoformat())
            return

        interval_minutes = max(1, int(cfg.get("interval_minutes", 30)))
        key = ("eis_public", company.id)
        last_run = self._last_run_by_source_company.get(key)
        if last_run is not None and now_ts - last_run < interval_minutes * 60:
            return

        self._last_run_by_source_company[key] = now_ts
        started = datetime.now(UTC)
        try:
            stats = await run_eis_public_ingestion(db, company.id, cfg)
            duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            logger.info(
                "ingestion company run: source=eis_public company_id=%s pages=%s candidates_total=%s inserted=%s updated=%s skipped=%s duration_ms=%s",
                company.id,
                stats.pages,
                stats.candidates_total,
                stats.inserted_count,
                stats.updated_count,
                stats.skipped_count,
                duration_ms,
            )
        except EISPublicMaintenanceError as exc:
            cooldown_until_new = datetime.now(UTC) + timedelta(hours=6)
            cfg_state = cfg.get("state") if isinstance(cfg.get("state"), dict) else {}
            cfg_state["cooldown_until"] = cooldown_until_new.isoformat().replace("+00:00", "Z")
            cfg["state"] = cfg_state
            settings["eis_public"] = cfg
            company.ingestion_settings = settings
            await db.commit()
            logger.warning(
                "eis_public maintenance detected, cooldown set: company_id=%s until=%s reason=%s",
                company.id,
                cfg_state["cooldown_until"],
                str(exc),
            )
        except Exception:
            logger.exception("ingestion company run failed: source=eis_public company_id=%s", company.id)

    async def _run_eis_opendata_if_due(self, db, company: Company, now_ts: float) -> None:
        cfg_raw = ((company.ingestion_settings or {}).get("eis_opendata") or {}) if company.ingestion_settings else {}
        if not isinstance(cfg_raw, dict):
            return

        try:
            cfg = EISOpenDataSettings.model_validate(cfg_raw)
        except Exception:
            logger.warning("EIS_OPENDATA error: company_id=%s reason=invalid_settings", company.id)
            return

        if not cfg.enabled:
            return

        interval_minutes = max(1, cfg.interval_minutes)
        key = ("eis_opendata", company.id)
        last_run = self._last_run_by_source_company.get(key)
        if last_run is not None and now_ts - last_run < interval_minutes * 60:
            return

        self._last_run_by_source_company[key] = now_ts
        started = datetime.now(UTC)
        try:
            stats = await run_eis_opendata_ingestion(db=db, company=company, settings=cfg)
            duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            logger.info(
                "ingestion company run: source=eis_opendata company_id=%s datasets=%s files=%s inserted=%s updated=%s skipped=%s duration_ms=%s",
                company.id,
                stats.datasets_count,
                stats.files_count,
                stats.inserted_count,
                stats.updated_count,
                stats.skipped_count,
                duration_ms,
            )
        except Exception:
            logger.exception("EIS_OPENDATA error: company_id=%s reason=job_failed", company.id)


scheduler = IngestionScheduler()


def _parse_dt(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None
