import asyncio
import copy
import logging
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.ingestion.eis_opendata.client import EISOpenDataClient
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
        self._last_run_stats: dict[tuple[str, UUID], dict] = {}

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
            self._last_run_stats[("eis_public", company.id)] = {
                "status": "cooldown",
                "cooldown_until": cooldown_until.isoformat().replace("+00:00", "Z"),
                "updated_at": now_utc.isoformat().replace("+00:00", "Z"),
            }
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
            self._last_run_stats[("eis_public", company.id)] = {
                "status": "ok",
                "pages": stats.pages,
                "candidates": stats.candidates_total,
                "inserted": stats.inserted_count,
                "updated": stats.updated_count,
                "skipped": stats.skipped_count,
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
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
            self._last_run_stats[("eis_public", company.id)] = {
                "status": "maintenance",
                "cooldown_until": cfg_state["cooldown_until"],
                "last_error": str(exc),
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        except Exception:
            logger.exception("ingestion company run failed: source=eis_public company_id=%s", company.id)

    async def _run_eis_opendata_if_due(self, db, company: Company, now_ts: float) -> None:
        settings = copy.deepcopy(company.ingestion_settings or {})
        cfg_raw = settings.get("eis_opendata") or {}
        if not isinstance(cfg_raw, dict):
            return

        try:
            cfg = EISOpenDataSettings.model_validate(cfg_raw)
        except Exception:
            logger.warning("EIS_OPENDATA error: company_id=%s reason=invalid_settings", company.id)
            return

        if not cfg.enabled:
            return

        now_utc = datetime.now(UTC)
        discovery = cfg.state.discovery

        cooldown_until = discovery.cooldown_until
        if cooldown_until and cooldown_until > now_utc:
            logger.info("EIS_OPENDATA discovery: status=maintenance reason=cooldown company_id=%s until=%s", company.id, cooldown_until.isoformat())
            self._last_run_stats[("eis_opendata", company.id)] = {
                "status": "maintenance",
                "cooldown_until": cooldown_until.isoformat().replace("+00:00", "Z"),
                "updated_at": now_utc.isoformat().replace("+00:00", "Z"),
            }
            return

        interval_minutes = max(1, cfg.interval_minutes)
        key = ("eis_opendata", company.id)
        last_run = self._last_run_by_source_company.get(key)
        if last_run is not None and now_ts - last_run < interval_minutes * 60:
            return

        self._last_run_by_source_company[key] = now_ts

        client = EISOpenDataClient(
            timeout_sec=cfg.download_timeout_sec,
            rate_limit_rps=cfg.rate_limit_rps,
            search_api_url=discovery.search_api_url,
            dataset_api_url=discovery.dataset_api_url,
        )

        try:
            need_discovery = not discovery.search_api_url
            recheck_due = (discovery.last_attempt_at is None) or ((now_utc - discovery.last_attempt_at) >= timedelta(minutes=30))

            if need_discovery or recheck_due:
                previous_status = discovery.status
                discovery.last_attempt_at = now_utc
                result = await client.discover_endpoints()
                discovery.status = result.status
                discovery.last_error = result.last_error

                if result.status == "maintenance":
                    discovery.cooldown_until = now_utc + timedelta(hours=6)
                    logger.warning(
                        "EIS_OPENDATA maintenance detected, cooldown set until %s company_id=%s",
                        discovery.cooldown_until.isoformat().replace("+00:00", "Z"),
                        company.id,
                    )
                    await self._save_opendata_settings(db, company, settings, cfg)
                    self._last_run_stats[("eis_opendata", company.id)] = {
                        "status": "maintenance",
                        "cooldown_until": discovery.cooldown_until.isoformat().replace("+00:00", "Z"),
                        "last_error": discovery.last_error,
                        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    }
                    return

                if result.status == "ok" and result.search_api_url:
                    discovery.search_api_url = result.search_api_url
                    if result.dataset_api_url:
                        discovery.dataset_api_url = result.dataset_api_url
                    discovery.last_success_at = now_utc
                    discovery.cooldown_until = None
                    if previous_status != "ok":
                        logger.info(
                            "EIS_OPENDATA recovered: search_api_url=%s dataset_api_url=%s",
                            discovery.search_api_url,
                            discovery.dataset_api_url,
                        )
                else:
                    logger.warning("EIS_OPENDATA discovery: status=%s reason=%s", discovery.status, discovery.last_error)

            if not discovery.search_api_url:
                await self._save_opendata_settings(db, company, settings, cfg)
                self._last_run_stats[("eis_opendata", company.id)] = {
                    "status": discovery.status,
                    "last_error": discovery.last_error,
                    "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
                return

            probe = await client.probe_search_endpoint(discovery.search_api_url, q="тест", limit=1)
            if not probe.ok:
                if probe.status == "maintenance":
                    discovery.status = "maintenance"
                    discovery.last_error = probe.last_error
                    discovery.cooldown_until = now_utc + timedelta(hours=6)
                    logger.warning(
                        "EIS_OPENDATA maintenance detected, cooldown set until %s company_id=%s",
                        discovery.cooldown_until.isoformat().replace("+00:00", "Z"),
                        company.id,
                    )
                else:
                    discovery.status = "unknown"
                    discovery.last_error = probe.last_error
                    logger.warning("EIS_OPENDATA discovery: status=unknown reason=%s", probe.last_error)
                await self._save_opendata_settings(db, company, settings, cfg)
                self._last_run_stats[("eis_opendata", company.id)] = {
                    "status": discovery.status,
                    "last_error": discovery.last_error,
                    "cooldown_until": discovery.cooldown_until.isoformat().replace("+00:00", "Z") if discovery.cooldown_until else None,
                    "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
                return

            discovery.status = "ok"
            discovery.last_success_at = now_utc
            discovery.cooldown_until = None
            await self._save_opendata_settings(db, company, settings, cfg)

            started = datetime.now(UTC)
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
            self._last_run_stats[("eis_opendata", company.id)] = {
                "status": "ok",
                "datasets": stats.datasets_count,
                "files": stats.files_count,
                "candidates": stats.candidates_count,
                "inserted": stats.inserted_count,
                "updated": stats.updated_count,
                "skipped": stats.skipped_count,
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        except Exception:
            logger.exception("EIS_OPENDATA error: company_id=%s reason=job_failed", company.id)
            self._last_run_stats[("eis_opendata", company.id)] = {
                "status": "error",
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        finally:
            await client.close()

    async def _save_opendata_settings(self, db, company: Company, settings: dict, cfg: EISOpenDataSettings) -> None:
        settings["eis_opendata"] = cfg.model_dump(mode="json")
        company.ingestion_settings = settings
        await db.commit()

    def get_health_snapshot(self) -> dict:
        items: list[dict] = []
        for (source, company_id), stat in self._last_run_stats.items():
            row = {"source": source, "company_id": str(company_id)}
            row.update(stat)
            items.append(row)
        return {"last_runs": items}


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
