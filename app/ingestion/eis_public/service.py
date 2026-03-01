import logging
import time
from datetime import UTC
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.eis_public.client import EISPublicClient
from app.ingestion.eis_public.parser import parse_search_results, parse_viewxml
from app.ingestion.eis_public.schemas import EISCandidate
from app.ingestion.interfaces import IngestionRunStats
from app.tenders.model import Tender

logger = logging.getLogger("uvicorn.error")

EIS_SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"


def _build_search_params(settings: dict, page: int) -> dict:
    params: dict[str, str | int] = {
        "searchString": settings.get("query", ""),
        "pageNumber": page,
        "recordsPerPage": int(settings.get("page_size", 50)),
    }
    if settings.get("only_active", True):
        params["af"] = "on"

    laws = settings.get("law") or []
    if isinstance(laws, list) and laws:
        params["law"] = ",".join(laws)

    regions = settings.get("regions") or []
    if isinstance(regions, list) and regions:
        params["region"] = ",".join(regions)

    return params


def _merge_candidate(tender: Tender, candidate: EISCandidate) -> bool:
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


async def upsert_tenders(company_id: UUID, candidates: list[EISCandidate], db: AsyncSession) -> tuple[int, int, int]:
    inserted = 0
    updated = 0
    skipped = 0

    for cand in candidates:
        if not cand.external_id:
            skipped += 1
            continue

        existing = await db.scalar(
            select(Tender).where(
                Tender.company_id == company_id,
                Tender.source == "eis",
                Tender.external_id == cand.external_id,
            )
        )

        if existing is None:
            tender = Tender(
                company_id=company_id,
                source="eis",
                external_id=cand.external_id,
                title=cand.title,
                customer_name=cand.customer_name,
                region=cand.region,
                procurement_type=cand.procurement_type,
                nmck=cand.nmck,
                published_at=cand.published_at,
                submission_deadline=cand.submission_deadline,
                status="new",
            )
            db.add(tender)
            inserted += 1
        else:
            if _merge_candidate(existing, cand):
                updated += 1
            else:
                skipped += 1

    await db.commit()
    return inserted, updated, skipped


async def run_eis_public_ingestion(db: AsyncSession, company_id: UUID, settings: dict) -> IngestionRunStats:
    start = time.perf_counter()
    max_pages = max(1, int(settings.get("max_pages", 2)))
    viewxml_limit = min(20, int(settings.get("viewxml_limit", 20)))

    client = EISPublicClient(
        timeout_sec=int(settings.get("timeout_sec", 20)),
        rate_limit_rps=float(settings.get("rate_limit_rps", 0.5)),
    )

    pages_done = 0
    candidates: list[EISCandidate] = []

    try:
        for page in range(1, max_pages + 1):
            params = _build_search_params(settings, page)
            html_text = await client.get_text(EIS_SEARCH_URL, params=params)
            pages_done += 1
            if not html_text:
                continue

            page_candidates = parse_search_results(html_text, EIS_SEARCH_URL)
            candidates.extend(page_candidates)

        if candidates:
            seen = set()
            deduped: list[EISCandidate] = []
            for c in candidates:
                if c.external_id in seen:
                    continue
                seen.add(c.external_id)
                deduped.append(c)
            candidates = deduped

        parsed_viewxml = 0
        for cand in candidates:
            if parsed_viewxml >= viewxml_limit:
                break
            if not cand.url_to_viewxml:
                continue
            xml_text = await client.get_text(cand.url_to_viewxml)
            if not xml_text:
                continue

            parsed = parse_viewxml(xml_text)
            if parsed is None:
                continue

            if parsed.external_id == cand.external_id:
                for field in ["title", "customer_name", "region", "procurement_type", "nmck", "published_at", "submission_deadline"]:
                    value = getattr(parsed, field)
                    if value is not None:
                        setattr(cand, field, value)
                parsed_viewxml += 1

        inserted, updated, skipped = await upsert_tenders(company_id, candidates, db)

        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "EIS ingestion done: inserted=%s updated=%s skipped=%s company_id=%s pages=%s candidates_total=%s duration_ms=%s",
            inserted,
            updated,
            skipped,
            company_id,
            pages_done,
            len(candidates),
            duration_ms,
        )

        return IngestionRunStats(
            pages=pages_done,
            candidates_total=len(candidates),
            inserted_count=inserted,
            updated_count=updated,
            skipped_count=skipped,
        )
    finally:
        await client.close()
