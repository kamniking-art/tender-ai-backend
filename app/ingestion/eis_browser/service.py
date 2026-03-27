from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.ingestion.eis_browser.client import EISBrowserClient
from app.ingestion.eis_site.parser import EISSiteCandidate
from app.models import Company
from app.tenders.model import Tender

logger = logging.getLogger("uvicorn.error")


@dataclass
class EISBrowserRunStats:
    stage: str = "init"
    reason: str | None = None
    source_status: str = "ok"
    pages: int = 0
    candidates: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    error_count: int = 0
    errors_sample: list[str] = field(default_factory=list)
    last_run_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _state_path_for_company(company_id: UUID) -> str:
    root = Path(settings.eis_browser_state_dir)
    root.mkdir(parents=True, exist_ok=True)
    return str(root / f"{company_id}.json")


async def run_eis_browser_once_for_company(
    db: AsyncSession,
    company: Company,
    *,
    query: str | None = None,
    pages: int = 5,
    page_size: int = 20,
    limit: int = 50,
    region: str | None = None,
) -> EISBrowserRunStats:
    stats = EISBrowserRunStats()
    cfg = _extract_settings(company.ingestion_settings if isinstance(company.ingestion_settings, dict) else {})
    query_value = (query if query is not None else cfg.get("query") or settings.eis_site_queries_list[0]).strip()
    pages_value = max(1, min(5, int(pages or cfg.get("max_pages", 5))))
    page_size_value = max(10, min(50, int(page_size or cfg.get("records_per_page", 20))))
    limit_value = max(1, min(50, int(limit or cfg.get("limit", 50))))

    client = EISBrowserClient(
        state_path=_state_path_for_company(company.id),
        timeout_ms=int(cfg.get("timeout_ms", 20000) or 20000),
    )

    stats.stage = "browser_start"
    candidates = await client.fetch_candidates(
        query=query_value,
        pages=pages_value,
        page_size=page_size_value,
        limit=limit_value,
        region=region or cfg.get("region"),
    )

    stats.stage = client.diagnostics.stage
    stats.source_status = client.diagnostics.source_status
    stats.reason = client.diagnostics.reason
    stats.pages = client.diagnostics.pages_opened
    stats.candidates = len(candidates)
    stats.error_count = client.diagnostics.error_count
    stats.errors_sample = list(client.diagnostics.errors_sample)

    for cand in candidates:
        outcome = await _upsert_tender(db, company.id, cand)
        if outcome == "inserted":
            stats.inserted += 1
        elif outcome == "updated":
            stats.updated += 1
        else:
            stats.skipped += 1

    stats.stage = "done" if stats.source_status != "error" else "error"
    if stats.reason is None:
        stats.reason = "ok"

    payload = copy.deepcopy(company.ingestion_settings) if isinstance(company.ingestion_settings, dict) else {}
    eis_browser = payload.get("eis_browser") if isinstance(payload.get("eis_browser"), dict) else {}
    state = eis_browser.get("state") if isinstance(eis_browser.get("state"), dict) else {}
    state.update(
        {
            "last_run_at": stats.last_run_at.isoformat(),
            "source_status": stats.source_status,
            "stage": stats.stage,
            "reason": stats.reason,
            "found": stats.candidates,
            "imported": stats.inserted,
            "updated": stats.updated,
            "error_count": stats.error_count,
        }
    )
    eis_browser["state"] = state
    payload["eis_browser"] = eis_browser
    company.ingestion_settings = payload

    await db.commit()
    logger.info(
        "EIS_BROWSER run: company_id=%s stage=%s source_status=%s pages=%s candidates=%s inserted=%s updated=%s skipped=%s",
        company.id,
        stats.stage,
        stats.source_status,
        stats.pages,
        stats.candidates,
        stats.inserted,
        stats.updated,
        stats.skipped,
    )
    return stats


def _extract_settings(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("eis_browser")
    if isinstance(raw, dict):
        return raw
    return {}


async def _upsert_tender(db: AsyncSession, company_id: UUID, candidate: EISSiteCandidate) -> str:
    existing = await db.scalar(
        select(Tender).where(
            Tender.company_id == company_id,
            Tender.source == "eis_browser",
            Tender.external_id == candidate.external_id,
        )
    )
    if existing is None:
        db.add(
            Tender(
                company_id=company_id,
                source="eis_browser",
                external_id=candidate.external_id,
                source_url=candidate.url,
                title=candidate.title,
                customer_name=candidate.customer_name,
                region=candidate.region,
                place_text=candidate.place_text,
                nmck=_normalize_decimal(candidate.nmck),
                published_at=candidate.published_at,
                submission_deadline=candidate.submission_deadline,
                status="new",
            )
        )
        return "inserted"

    changed = False
    for field in ("title", "customer_name", "region", "place_text", "published_at", "submission_deadline", "source_url"):
        value = getattr(candidate, field if field != "source_url" else "url")
        if value is not None and getattr(existing, field) != value:
            setattr(existing, field, value)
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
