from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.eis_site.client import EISSiteClient
from app.ingestion.eis_site.parser import EISSiteCandidate, parse_search_page
from app.models import Company
from app.tenders.model import Tender

logger = logging.getLogger("uvicorn.error")

EIS_SITE_SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"


@dataclass
class EISSiteRunStats:
    stage: str = "fetch"
    reason: str | None = None
    source_status: str = "ok"
    http_status: int | None = None
    fetched_bytes: int = 0
    error_count: int = 0
    errors_sample: list[str] = field(default_factory=list)
    pages: int = 0
    candidates: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


@dataclass
class EISSiteSettings:
    query: str = ""
    limit: int = 50
    region: str | None = None
    timeout_sec: int = 20
    rate_limit_rps: float = 0.4
    records_per_page: int = 50


async def run_eis_site_once_for_company(
    db: AsyncSession,
    company: Company,
    *,
    query: str | None = None,
    limit: int = 50,
    region: str | None = None,
) -> EISSiteRunStats:
    cfg = _extract_settings(company.ingestion_settings or {})
    if query is not None:
        cfg.query = query
    cfg.limit = max(1, min(200, int(limit or cfg.limit)))
    if region is not None:
        cfg.region = region or None

    client = EISSiteClient(timeout_sec=cfg.timeout_sec, rate_limit_rps=cfg.rate_limit_rps)
    stats = EISSiteRunStats(stage="fetch")

    try:
        collected: list[EISSiteCandidate] = []
        page = 1
        while len(collected) < cfg.limit:
            params = _build_search_params(cfg, page)
            html_text = await client.fetch_search_page(EIS_SITE_SEARCH_URL, params=params)
            stats.pages += 1
            if html_text is None:
                stats.stage = "error"
                stats.reason = client.diagnostics.reason or "fetch_failed"
                stats.source_status = client.diagnostics.source_status
                break

            stats.stage = "parse"
            parsed = parse_search_page(html_text, EIS_SITE_SEARCH_URL)
            if parsed.errors:
                stats.errors_sample.extend(parsed.errors[: max(0, 3 - len(stats.errors_sample))])

            if not parsed.candidates:
                if page == 1:
                    stats.reason = "no_results"
                break

            collected.extend(parsed.candidates)
            if len(parsed.candidates) < cfg.records_per_page:
                break
            page += 1
            if page > 5:
                break

        deduped: list[EISSiteCandidate] = []
        seen: set[str] = set()
        for item in collected:
            if not item.external_id or item.external_id in seen:
                continue
            seen.add(item.external_id)
            deduped.append(item)

        if stats.source_status == "ok" and stats.stage != "error":
            stats.stage = "insert"
            stats.candidates = min(len(deduped), cfg.limit)
            if stats.candidates == 0 and not stats.reason:
                stats.reason = "no_results"

            for cand in deduped[: cfg.limit]:
                outcome = await _upsert_tender(db, company.id, cand)
                if outcome == "inserted":
                    stats.inserted += 1
                elif outcome == "updated":
                    stats.updated += 1
                else:
                    stats.skipped += 1

            await db.commit()
            stats.stage = "done"
            if not stats.reason:
                stats.reason = "ok"

        stats.http_status = client.diagnostics.http_status
        stats.fetched_bytes = client.diagnostics.fetched_bytes
        stats.error_count += client.diagnostics.error_count
        if client.diagnostics.errors_sample:
            for msg in client.diagnostics.errors_sample:
                if len(stats.errors_sample) < 3 and msg not in stats.errors_sample:
                    stats.errors_sample.append(msg)

        logger.info(
            "EIS_SITE run: stage=%s reason=%s source_status=%s company_id=%s pages=%s candidates=%s inserted=%s updated=%s skipped=%s http_status=%s fetched_bytes=%s",
            stats.stage,
            stats.reason,
            stats.source_status,
            company.id,
            stats.pages,
            stats.candidates,
            stats.inserted,
            stats.updated,
            stats.skipped,
            stats.http_status,
            stats.fetched_bytes,
        )

        return stats
    finally:
        await client.close()


def _extract_settings(payload: dict[str, Any]) -> EISSiteSettings:
    raw = payload.get("eis_site") if isinstance(payload.get("eis_site"), dict) else {}
    return EISSiteSettings(
        query=str(raw.get("query", "") or ""),
        limit=int(raw.get("limit", 50) or 50),
        region=str(raw.get("region")) if raw.get("region") else None,
        timeout_sec=int(raw.get("timeout_sec", 20) or 20),
        rate_limit_rps=float(raw.get("rate_limit_rps", 0.4) or 0.4),
        records_per_page=max(10, min(50, int(raw.get("records_per_page", 50) or 50))),
    )


def _build_search_params(cfg: EISSiteSettings, page: int) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "searchString": cfg.query,
        "pageNumber": page,
        "recordsPerPage": cfg.records_per_page,
        "af": "on",
    }
    if cfg.region:
        params["region"] = cfg.region
    return params


async def _upsert_tender(db: AsyncSession, company_id: UUID, candidate: EISSiteCandidate) -> str:
    existing = await db.scalar(
        select(Tender).where(
            Tender.company_id == company_id,
            Tender.source == "eis_site",
            Tender.external_id == candidate.external_id,
        )
    )

    if existing is None:
        db.add(
            Tender(
                company_id=company_id,
                source="eis_site",
                external_id=candidate.external_id,
                source_url=candidate.url,
                title=candidate.title,
                customer_name=candidate.customer_name,
                nmck=_normalize_decimal(candidate.nmck),
                published_at=candidate.published_at,
                submission_deadline=candidate.submission_deadline,
                status="new",
            )
        )
        return "inserted"

    changed = False
    for field in ("title", "customer_name", "published_at", "submission_deadline"):
        value = getattr(candidate, field)
        if value is not None and getattr(existing, field) != value:
            setattr(existing, field, value)
            changed = True

    if candidate.url is not None and existing.source_url != candidate.url:
        existing.source_url = candidate.url
        changed = True

    nmck = _normalize_decimal(candidate.nmck)
    if nmck is not None and existing.nmck != nmck:
        existing.nmck = nmck
        changed = True

    return "updated" if changed else "skipped"


def _normalize_decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except Exception:
        return None
