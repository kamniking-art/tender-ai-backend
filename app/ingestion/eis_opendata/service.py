from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.eis_opendata.client import EISOpenDataClient, EISOpenDataMaintenanceError
from app.ingestion.eis_opendata.parser import iter_candidates_from_file
from app.ingestion.eis_opendata.schemas import EISDatasetSummary, EISOpenDataSettings, OpenDataCandidate
from app.ingestion.eis_opendata.state import mark_dataset_processed, should_process_resource
from app.models import Company
from app.tenders.model import Tender

logger = logging.getLogger("uvicorn.error")


@dataclass
class OpenDataRunStats:
    datasets_count: int = 0
    files_count: int = 0
    candidates_count: int = 0
    inserted_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0


async def list_available_datasets(settings: EISOpenDataSettings, q: str, limit: int = 20) -> list[EISDatasetSummary]:
    client = EISOpenDataClient(
        timeout_sec=settings.download_timeout_sec,
        rate_limit_rps=settings.rate_limit_rps,
        search_api_url=settings.state.discovery.search_api_url,
        dataset_api_url=settings.state.discovery.dataset_api_url,
    )
    try:
        datasets = await client.list_datasets(q=q, limit=limit)
        return [
            EISDatasetSummary(
                dataset_id=ds.dataset_id,
                title=ds.title,
                updated_at=ds.updated_at,
                files=[
                    {"name": r.name, "url": r.url, "updated_at": r.updated_at, "size": r.size, "format": r.format}
                    for r in ds.resources
                ],
            )
            for ds in datasets
        ]
    finally:
        await client.close()


async def run_eis_opendata_ingestion(db: AsyncSession, company: Company, settings: EISOpenDataSettings) -> OpenDataRunStats:
    start = time.perf_counter()
    stats = OpenDataRunStats()

    storage_dir = Path(settings.storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)

    client = EISOpenDataClient(
        timeout_sec=settings.download_timeout_sec,
        rate_limit_rps=settings.rate_limit_rps,
        search_api_url=settings.state.discovery.search_api_url,
        dataset_api_url=settings.state.discovery.dataset_api_url,
    )

    dataset_ids = list(settings.dataset_ids)

    try:
        if not dataset_ids:
            if not settings.allow_demo:
                logger.warning("EIS_OPENDATA run: inserted=0 updated=0 skipped=0 candidates=0 reason=dataset_ids_empty company_id=%s", company.id)
                return stats

            search_q = " OR ".join(settings.keywords) if settings.keywords else "закуп"
            demo_list = await client.search_datasets(q=search_q, limit=20)
            dataset_ids = [x.dataset_id for x in demo_list[:2]]
            if not dataset_ids:
                logger.warning("EIS_OPENDATA run: inserted=0 updated=0 skipped=0 candidates=0 reason=demo_no_datasets company_id=%s", company.id)
                return stats
            logger.info("dataset_ids empty, using demo datasets: %s", dataset_ids)

        for dataset_id in dataset_ids:
            dataset = await client.get_dataset(dataset_id)
            if dataset is None:
                logger.warning("EIS_OPENDATA error: dataset_id=%s reason=dataset_not_found", dataset_id)
                continue

            stats.datasets_count += 1
            processed_files = 0
            for resource in dataset.resources:
                if processed_files >= settings.max_files_per_run:
                    break
                if not should_process_resource(settings, dataset.dataset_id, resource):
                    continue

                file_name = _build_file_name(dataset.dataset_id, resource.url)
                download_path = storage_dir / file_name

                ok = await client.download_to(resource.url, download_path)
                if not ok:
                    logger.warning("EIS_OPENDATA error: dataset_id=%s reason=download_failed file=%s", dataset.dataset_id, resource.url)
                    continue

                inserted, updated, skipped, candidates = await _process_downloaded_file(
                    db=db,
                    company_id=company.id,
                    file_path=download_path,
                    settings=settings,
                )
                stats.candidates_count += candidates
                stats.inserted_count += inserted
                stats.updated_count += updated
                stats.skipped_count += skipped
                stats.files_count += 1
                processed_files += 1

                mark_dataset_processed(settings, dataset.dataset_id, resource)
                _cleanup_path(download_path)

        _persist_company_state(company, settings)
        await db.commit()

        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "EIS_OPENDATA run: inserted=%s updated=%s skipped=%s candidates=%s company_id=%s datasets=%s files=%s duration_ms=%s",
            stats.inserted_count,
            stats.updated_count,
            stats.skipped_count,
            stats.candidates_count,
            company.id,
            stats.datasets_count,
            stats.files_count,
            duration_ms,
        )
        return stats
    except EISOpenDataMaintenanceError as exc:
        logger.warning("EIS_OPENDATA run: inserted=0 updated=0 skipped=%s candidates=%s reason=%s company_id=%s", stats.skipped_count, stats.candidates_count, str(exc), company.id)
        return stats
    finally:
        await client.close()


async def run_eis_opendata_once_for_company(db: AsyncSession, company: Company) -> OpenDataRunStats:
    current = company.ingestion_settings or {}
    cfg_raw = current.get("eis_opendata") or {}
    settings = EISOpenDataSettings.model_validate(cfg_raw)
    return await run_eis_opendata_ingestion(db, company, settings)


async def _process_downloaded_file(
    db: AsyncSession,
    company_id: UUID,
    file_path: Path,
    settings: EISOpenDataSettings,
) -> tuple[int, int, int, int]:
    inserted = 0
    updated = 0
    skipped = 0
    candidates = 0

    candidates_iter, _ = iter_candidates_from_file(file_path, settings.max_records_per_file)
    for candidate in candidates_iter:
        candidates += 1
        if not _passes_filters(candidate, settings):
            skipped += 1
            continue

        if not candidate.external_id:
            skipped += 1
            continue

        existing = await db.scalar(
            select(Tender).where(
                Tender.company_id == company_id,
                Tender.source == "eis_opendata",
                Tender.external_id == candidate.external_id,
            )
        )

        if existing is None:
            db.add(
                Tender(
                    company_id=company_id,
                    source="eis_opendata",
                    external_id=candidate.external_id,
                    title=candidate.title,
                    customer_name=candidate.customer_name,
                    region=candidate.region,
                    procurement_type=candidate.procurement_type,
                    nmck=candidate.nmck,
                    published_at=candidate.published_at,
                    submission_deadline=candidate.submission_deadline,
                    status="new",
                )
            )
            inserted += 1
        else:
            if _merge_candidate(existing, candidate):
                updated += 1
            else:
                skipped += 1

    await db.flush()
    return inserted, updated, skipped, candidates


def _passes_filters(candidate: OpenDataCandidate, settings: EISOpenDataSettings) -> bool:
    if settings.keywords:
        title = (candidate.title or "").lower()
        keywords = [x.lower() for x in settings.keywords if x]
        if keywords and not any(word in title for word in keywords):
            return False

    if settings.regions and candidate.region:
        regions = {x.lower() for x in settings.regions if x}
        if regions and candidate.region.lower() not in regions:
            return False

    if settings.laws and candidate.procurement_type:
        laws = {x.lower() for x in settings.laws if x}
        if laws and candidate.procurement_type.lower() not in laws:
            return False

    return True


def _merge_candidate(tender: Tender, candidate: OpenDataCandidate) -> bool:
    changed = False
    for field in [
        "title",
        "customer_name",
        "region",
        "procurement_type",
        "nmck",
        "published_at",
        "submission_deadline",
    ]:
        value = getattr(candidate, field)
        if value is not None and getattr(tender, field) != value:
            setattr(tender, field, value)
            changed = True
    return changed


def _build_file_name(dataset_id: str, url: str) -> str:
    safe_dataset = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in dataset_id)[:80] or "dataset"
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0] or "download.bin"
    safe_tail = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in tail)[:120] or "download.bin"
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{safe_dataset}_{timestamp}_{safe_tail}"


def _persist_company_state(company: Company, settings: EISOpenDataSettings) -> None:
    current = dict(company.ingestion_settings or {})
    current["eis_opendata"] = settings.model_dump(mode="json")
    company.ingestion_settings = current


def _cleanup_path(path: Path) -> None:
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        logger.warning("eis_opendata cleanup warning: path=%s", path)
