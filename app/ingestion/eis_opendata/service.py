from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.ingestion.eis_opendata.client import EISOpenDataClient, EISOpenDataMaintenanceError
from app.ingestion.eis_opendata.parser import iter_candidates_from_file
from app.ingestion.eis_opendata.schemas import DatasetMeta, EISDatasetSummary, EISOpenDataSettings, OpenDataCandidate
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
    stage: str = "discover"
    reason: str | None = None
    source_status: str = "ok"
    catalog_url: str | None = None
    http_status: int | None = None
    error_count: int = 0
    errors_sample: list[str] = field(default_factory=list)


@dataclass
class OpenDataDatasetsResult:
    source_status: str
    reason: str | None
    catalog_url: str | None
    http_status: int | None
    items: list[EISDatasetSummary]
    error_count: int = 0
    errors_sample: list[str] = field(default_factory=list)


async def list_available_datasets(settings: EISOpenDataSettings, q: str, limit: int = 20) -> OpenDataDatasetsResult:
    client = EISOpenDataClient(
        timeout_sec=settings.download_timeout_sec,
        rate_limit_rps=settings.rate_limit_rps,
        search_api_url=settings.state.discovery.search_api_url,
        dataset_api_url=settings.state.discovery.dataset_api_url,
    )
    try:
        datasets = await client.list_datasets(q=q, limit=limit)
        if not datasets and app_settings.allow_known_datasets_fallback and app_settings.known_datasets_list:
            datasets = [
                await _build_known_dataset_meta(client=client, dataset_ref=dataset_ref)
                for dataset_ref in app_settings.known_datasets_list[:3]
            ]
        items = [
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
        diag = client.get_diagnostics()
        reason = diag.reason if items == [] else None
        return OpenDataDatasetsResult(
            source_status=diag.source_status,
            reason=reason,
            catalog_url=diag.catalog_url,
            http_status=diag.http_status,
            items=items,
            error_count=diag.error_count,
            errors_sample=diag.errors_sample[:3],
        )
    except EISOpenDataMaintenanceError as exc:
        diag = client.get_diagnostics()
        return OpenDataDatasetsResult(
            source_status="maintenance",
            reason=exc.reason,
            catalog_url=diag.catalog_url,
            http_status=exc.http_status or diag.http_status,
            items=[],
            error_count=diag.error_count,
            errors_sample=diag.errors_sample[:3],
        )
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
    stats.catalog_url = client.get_diagnostics().catalog_url

    try:
        if not dataset_ids:
            if not settings.allow_demo:
                if app_settings.allow_known_datasets_fallback and app_settings.known_datasets_list:
                    dataset_ids = app_settings.known_datasets_list[:3]
                    stats.reason = "using_known_datasets_fallback"
                else:
                    stats.reason = "dataset_ids_empty"
                    stats.source_status = "error"
                    stats.stage = "discover"
                    logger.warning("EIS_OPENDATA run: inserted=0 updated=0 skipped=0 candidates=0 reason=dataset_ids_empty company_id=%s", company.id)
                    _fill_stats_from_client(stats, client)
                    return stats

            if not dataset_ids:
                search_q = " OR ".join(settings.keywords) if settings.keywords else "закуп"
                demo_list = await client.search_datasets(q=search_q, limit=20)
                dataset_ids = [x.dataset_id for x in demo_list[:2]]
                if not dataset_ids:
                    stats.reason = "no_datasets_match_query"
                    stats.source_status = "error"
                    stats.stage = "discover"
                    logger.warning("EIS_OPENDATA run: inserted=0 updated=0 skipped=0 candidates=0 reason=demo_no_datasets company_id=%s", company.id)
                    _fill_stats_from_client(stats, client)
                    return stats
                logger.info("dataset_ids empty, using demo datasets: %s", dataset_ids)

        stats.stage = "download"
        for dataset_id in dataset_ids:
            dataset = await client.get_dataset(dataset_id)
            if dataset is None:
                logger.warning("EIS_OPENDATA error: dataset_id=%s reason=dataset_not_found", dataset_id)
                stats.error_count += 1
                if len(stats.errors_sample) < 3:
                    stats.errors_sample.append(f"dataset_not_found:{dataset_id}")
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
                    stats.error_count += 1
                    if len(stats.errors_sample) < 3:
                        stats.errors_sample.append(f"download_failed:{dataset.dataset_id}")
                    continue

                stats.stage = "parse"
                source_name = "fallback" if stats.reason == "using_known_datasets_fallback" else "eis_opendata"
                inserted, updated, skipped, candidates = await _process_downloaded_file(
                    db=db,
                    company_id=company.id,
                    file_path=download_path,
                    settings=settings,
                    source_name=source_name,
                )
                stats.stage = "insert"
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
        _fill_stats_from_client(stats, client)
        stats.stage = "done"
        if stats.reason is None:
            stats.reason = "ok"
        if stats.source_status == "ok" and stats.datasets_count == 0:
            stats.reason = "no_datasets_match_query"

        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "EIS_OPENDATA run: stage=%s reason=%s source_status=%s inserted=%s updated=%s skipped=%s candidates=%s company_id=%s datasets=%s files=%s duration_ms=%s",
            stats.stage,
            stats.reason,
            stats.source_status,
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
        stats.stage = "discover"
        stats.reason = exc.reason
        stats.source_status = "maintenance"
        stats.http_status = exc.http_status
        _fill_stats_from_client(stats, client)
        logger.warning("EIS_OPENDATA run: inserted=0 updated=0 skipped=%s candidates=%s reason=%s company_id=%s", stats.skipped_count, stats.candidates_count, str(exc), company.id)
        return stats
    except Exception as exc:
        stats.stage = "error"
        stats.reason = "job_failed"
        stats.source_status = "error"
        stats.error_count += 1
        if len(stats.errors_sample) < 3:
            stats.errors_sample.append(exc.__class__.__name__)
        _fill_stats_from_client(stats, client)
        raise
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
    source_name: str,
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
                Tender.source == source_name,
                Tender.external_id == candidate.external_id,
            )
        )

        if existing is None:
            db.add(
                Tender(
                    company_id=company_id,
                    source=source_name,
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


def _fill_stats_from_client(stats: OpenDataRunStats, client: EISOpenDataClient) -> None:
    diag = client.get_diagnostics()
    stats.catalog_url = diag.catalog_url
    stats.http_status = diag.http_status
    if stats.source_status == "ok":
        stats.source_status = diag.source_status
    if stats.reason is None:
        stats.reason = diag.reason
    stats.error_count = max(stats.error_count, diag.error_count)
    for item in diag.errors_sample:
        if len(stats.errors_sample) >= 3:
            break
        if item not in stats.errors_sample:
            stats.errors_sample.append(item)


async def _build_known_dataset_meta(client: EISOpenDataClient, dataset_ref: str) -> DatasetMeta:
    dataset = await client.get_dataset(dataset_ref)
    if dataset is not None:
        return dataset
    return DatasetMeta(
        dataset_id=dataset_ref,
        title="Known dataset fallback",
        updated_at=None,
        resources=[],
    )
