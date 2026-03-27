from __future__ import annotations

import logging
import random
import asyncio
import copy
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.eis_site.client import EISSiteClient
from app.ingestion.eis_site.parser import EISSiteCandidate, parse_search_page
from app.core.config import settings
from app.models import Company
from app.tenders.model import Tender

logger = logging.getLogger("uvicorn.error")

EIS_SITE_SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"
_SPACE_RE = re.compile(r"\s+")

_REGION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "санкт петербург": ("спб", "питер", "saint petersburg", "st petersburg", "sankt peterburg"),
    "ленинградская область": ("лен область", "ленобласть", "leningrad oblast", "leningrad region"),
    "кингисепп": ("kingisepp", "кингисеппский район", "kingisepp district"),
}


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
    region_filter: str | None = None
    region_filter_applied: bool = False
    candidates_before_region_filter: int = 0
    candidates_after_region_filter: int = 0
    cooldown_until: datetime | None = None


@dataclass
class EISSiteBulkQueryStats:
    query: str
    stage: str
    reason: str | None
    source_status: str
    pages: int
    candidates: int
    inserted: int
    updated: int
    skipped: int


@dataclass
class EISSiteBulkRunStats:
    totals: EISSiteRunStats = field(default_factory=EISSiteRunStats)
    breakdown: list[EISSiteBulkQueryStats] = field(default_factory=list)
    blocked_count: int = 0
    maintenance_count: int = 0


@dataclass
class EISSiteSettings:
    query: str = ""
    limit: int = 1000
    max_pages: int = 50
    region: str | None = None
    timeout_sec: int = 20
    min_request_delay_sec: float = 2.0
    max_request_delay_sec: float = 3.5
    page_delay_min_sec: float = 1.8
    page_delay_max_sec: float = 4.2
    long_pause_min_sec: float = 8.0
    long_pause_max_sec: float = 15.0
    long_pause_every_min_pages: int = 10
    long_pause_every_max_pages: int = 15
    records_per_page: int = 20


class SourceStatus(StrEnum):
    OK = "ok"
    COOLDOWN = "cooldown"
    BLOCKED = "blocked"


async def run_eis_site_once_for_company(
    db: AsyncSession,
    company: Company,
    *,
    query: str | None = None,
    limit: int = 1000,
    pages: int | None = None,
    page_size: int | None = None,
    region: str | None = None,
    dedupe_mode: str = "update",
) -> EISSiteRunStats:
    payload = copy.deepcopy(company.ingestion_settings) if isinstance(company.ingestion_settings, dict) else {}
    cfg = _extract_settings(payload)
    if query is not None:
        cfg.query = query
    cfg.limit = max(1, min(5000, int(limit or cfg.limit)))
    if pages is not None:
        cfg.max_pages = max(1, min(200, int(pages)))
    if page_size is not None:
        cfg.records_per_page = max(10, min(50, int(page_size)))
    if region is not None:
        cfg.region = region or None

    client = EISSiteClient(
        timeout_sec=cfg.timeout_sec,
        min_request_delay_sec=cfg.min_request_delay_sec,
        max_request_delay_sec=cfg.max_request_delay_sec,
    )
    stats = EISSiteRunStats(stage="fetch")
    max_age_days = max(1, int(settings.eis_site_max_age_days or 60))
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    now_utc = datetime.now(UTC)
    state = _get_source_state(payload)
    cooldown_until = _parse_dt(state.get("cooldown_until"))
    state_changed = False

    if cooldown_until is not None and cooldown_until > now_utc:
        stats.stage = "error"
        stats.reason = "cooldown_active"
        stats.source_status = SourceStatus.COOLDOWN.value
        stats.cooldown_until = cooldown_until
        logger.warning(
            "EIS_SITE source cooldown active: company_id=%s until=%s",
            company.id,
            cooldown_until.isoformat(),
        )
        return stats

    if cooldown_until is not None and cooldown_until <= now_utc:
        _set_source_state(
            payload,
            status=SourceStatus.OK,
            last_block_time=state.get("last_block_time"),
            cooldown_until=None,
        )
        company.ingestion_settings = payload
        state_changed = True

    try:
        collected: list[EISSiteCandidate] = []
        next_long_pause_after = random.randint(cfg.long_pause_every_min_pages, cfg.long_pause_every_max_pages)
        pages_since_long_pause = 0
        for page in range(1, cfg.max_pages + 1):
            if len(collected) >= cfg.limit:
                break
            params = _build_search_params(cfg, page)
            html_text = await client.fetch_search_page(EIS_SITE_SEARCH_URL, params=params, page_number=page)
            stats.pages += 1
            if html_text is None:
                stats.stage = "error"
                stats.reason = client.diagnostics.reason or "fetch_failed"
                stats.source_status = client.diagnostics.source_status
                if stats.source_status == SourceStatus.BLOCKED.value:
                    blocked_at = datetime.now(UTC)
                    cooldown_until = blocked_at + timedelta(minutes=max(30, settings.eis_source_blocked_cooldown_minutes))
                    stats.cooldown_until = cooldown_until
                    _set_source_state(
                        payload,
                        status=SourceStatus.BLOCKED,
                        last_block_time=blocked_at,
                        cooldown_until=cooldown_until,
                    )
                    company.ingestion_settings = payload
                    state_changed = True
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
            pages_since_long_pause += 1
            await asyncio.sleep(random.uniform(cfg.page_delay_min_sec, cfg.page_delay_max_sec))
            if pages_since_long_pause >= next_long_pause_after:
                await asyncio.sleep(random.uniform(cfg.long_pause_min_sec, cfg.long_pause_max_sec))
                pages_since_long_pause = 0
                next_long_pause_after = random.randint(cfg.long_pause_every_min_pages, cfg.long_pause_every_max_pages)

        deduped: list[EISSiteCandidate] = []
        seen: set[str] = set()
        for item in collected:
            if not item.external_id or item.external_id in seen:
                continue
            # Skip archive tenders to keep operational list fresh.
            if item.published_at is not None and item.published_at < cutoff:
                stats.skipped += 1
                continue
            seen.add(item.external_id)
            deduped.append(item)

        stats.region_filter = cfg.region
        if stats.source_status == "ok" and stats.stage != "error":
            stats.stage = "insert"
            region_filtered = deduped
            stats.candidates_before_region_filter = len(deduped)
            if cfg.region:
                stats.region_filter_applied = True
                region_filtered = [cand for cand in deduped if _candidate_matches_region(cand, cfg.region or "")]
            stats.candidates_after_region_filter = len(region_filtered)
            stats.candidates = min(len(region_filtered), cfg.limit)
            if stats.candidates == 0 and not stats.reason:
                stats.reason = "no_results"

            update_existing = (dedupe_mode or "update").strip().lower() != "skip"
            for cand in region_filtered[: cfg.limit]:
                outcome = await _upsert_tender(db, company.id, cand, update_existing=update_existing)
                if outcome == "inserted":
                    stats.inserted += 1
                elif outcome == "updated":
                    stats.updated += 1
                else:
                    stats.skipped += 1

            await db.commit()
            if state_changed:
                state_changed = False
            stats.stage = "done"
            if not stats.reason:
                stats.reason = "ok"
            _set_source_state(payload, status=SourceStatus.OK, last_block_time=state.get("last_block_time"), cooldown_until=None)
            company.ingestion_settings = payload
            state_changed = True

        stats.http_status = client.diagnostics.http_status
        stats.fetched_bytes = client.diagnostics.fetched_bytes
        stats.error_count += client.diagnostics.error_count
        if client.diagnostics.errors_sample:
            for msg in client.diagnostics.errors_sample:
                if len(stats.errors_sample) < 3 and msg not in stats.errors_sample:
                    stats.errors_sample.append(msg)

        logger.info(
            "EIS_SITE run: stage=%s reason=%s source_status=%s company_id=%s pages=%s candidates=%s inserted=%s updated=%s skipped=%s region_filter=%s region_filter_applied=%s candidates_before_region_filter=%s candidates_after_region_filter=%s http_status=%s fetched_bytes=%s cooldown_until=%s",
            stats.stage,
            stats.reason,
            stats.source_status,
            company.id,
            stats.pages,
            stats.candidates,
            stats.inserted,
            stats.updated,
            stats.skipped,
            stats.region_filter,
            stats.region_filter_applied,
            stats.candidates_before_region_filter,
            stats.candidates_after_region_filter,
            stats.http_status,
            stats.fetched_bytes,
            stats.cooldown_until.isoformat() if stats.cooldown_until else "-",
        )

        if state_changed:
            await db.commit()
        return stats
    finally:
        await client.close()


def _extract_settings(payload: dict[str, Any]) -> EISSiteSettings:
    raw = payload.get("eis_site") if isinstance(payload.get("eis_site"), dict) else {}
    return EISSiteSettings(
        query=str(raw.get("query", "") or ""),
        limit=int(raw.get("limit", 1000) or 1000),
        max_pages=max(1, min(200, int(raw.get("max_pages", 50) or 50))),
        region=str(raw.get("region")) if raw.get("region") else None,
        timeout_sec=int(raw.get("timeout_sec", 20) or 20),
        min_request_delay_sec=float(
            raw.get("min_request_delay_sec", settings.eis_source_request_delay_sec) or settings.eis_source_request_delay_sec
        ),
        max_request_delay_sec=float(
            raw.get(
                "max_request_delay_sec",
                settings.eis_source_request_delay_sec + settings.eis_source_request_jitter_sec,
            )
            or (settings.eis_source_request_delay_sec + settings.eis_source_request_jitter_sec)
        ),
        page_delay_min_sec=float(raw.get("page_delay_min_sec", settings.eis_source_page_delay_min_sec) or settings.eis_source_page_delay_min_sec),
        page_delay_max_sec=float(raw.get("page_delay_max_sec", settings.eis_source_page_delay_max_sec) or settings.eis_source_page_delay_max_sec),
        long_pause_min_sec=float(raw.get("long_pause_min_sec", settings.eis_source_long_pause_min_sec) or settings.eis_source_long_pause_min_sec),
        long_pause_max_sec=float(raw.get("long_pause_max_sec", settings.eis_source_long_pause_max_sec) or settings.eis_source_long_pause_max_sec),
        long_pause_every_min_pages=max(
            2,
            int(raw.get("long_pause_every_min_pages", settings.eis_source_long_pause_every_min_pages) or settings.eis_source_long_pause_every_min_pages),
        ),
        long_pause_every_max_pages=max(
            2,
            int(raw.get("long_pause_every_max_pages", settings.eis_source_long_pause_every_max_pages) or settings.eis_source_long_pause_every_max_pages),
        ),
        records_per_page=max(10, min(50, int(raw.get("records_per_page", 20) or 20))),
    )


def _get_source_state(payload: dict[str, Any]) -> dict[str, Any]:
    eis_site = payload.get("eis_site")
    if not isinstance(eis_site, dict):
        eis_site = {}
        payload["eis_site"] = eis_site
    state = eis_site.get("state")
    if not isinstance(state, dict):
        state = {}
        eis_site["state"] = state
    source = state.get("source")
    if not isinstance(source, dict):
        source = {}
        state["source"] = source
    return source


def _set_source_state(
    payload: dict[str, Any],
    *,
    status: SourceStatus,
    last_block_time: datetime | str | None,
    cooldown_until: datetime | None,
) -> None:
    source = _get_source_state(payload)
    source["source_status"] = status.value
    source["last_block_time"] = _to_iso(last_block_time)
    source["cooldown_until"] = _to_iso(cooldown_until)


def _to_iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.astimezone(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _normalize_region_text(value: str) -> str:
    text = (value or "").strip().lower()
    text = text.replace("ё", "е")
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    return _SPACE_RE.sub(" ", text).strip()


def _region_terms(value: str) -> set[str]:
    normalized = _normalize_region_text(value)
    if not normalized:
        return set()
    terms = {normalized}
    for base, aliases in _REGION_SYNONYMS.items():
        alias_norm = {_normalize_region_text(x) for x in aliases}
        if normalized == base or normalized in alias_norm or any(normalized in a for a in alias_norm) or base in normalized:
            terms.add(base)
            terms.update(alias_norm)
    return {t for t in terms if len(t) >= 2}


def _candidate_matches_region(candidate: EISSiteCandidate, region_filter: str) -> bool:
    terms = _region_terms(region_filter)
    if not terms:
        return True
    haystack_raw = " ".join(
        [
            candidate.region or "",
            candidate.place_text or "",
            candidate.customer_name or "",
            candidate.title or "",
        ]
    )
    haystack = _normalize_region_text(haystack_raw)
    if not haystack:
        return False
    return any(term in haystack for term in terms)


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


async def _upsert_tender(db: AsyncSession, company_id: UUID, candidate: EISSiteCandidate, *, update_existing: bool = True) -> str:
    source_url = candidate.url or _default_source_url(candidate.external_id)
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
                source_url=source_url,
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

    if not update_existing:
        return "skipped"

    changed = False
    for field in ("title", "customer_name", "region", "place_text", "published_at", "submission_deadline"):
        value = getattr(candidate, field)
        if value is not None and getattr(existing, field) != value:
            setattr(existing, field, value)
            changed = True

    if source_url is not None and existing.source_url != source_url:
        existing.source_url = source_url
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


def _default_source_url(external_id: str | None) -> str | None:
    if not external_id:
        return None
    return f"https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber={external_id}"


async def run_eis_site_bulk_for_company(
    db: AsyncSession,
    company: Company,
    *,
    queries: list[str],
    pages_per_query: int = 10,
    page_size: int = 20,
    dedupe_mode: str = "update",
    stop_if_blocked: bool = True,
) -> EISSiteBulkRunStats:
    clean_queries = [item.strip() for item in queries if item and item.strip()]
    if not clean_queries:
        return EISSiteBulkRunStats()

    mode = dedupe_mode.strip().lower() if dedupe_mode else "update"
    if mode not in {"update", "skip"}:
        mode = "update"

    bulk = EISSiteBulkRunStats()
    for query in clean_queries:
        stats = await run_eis_site_once_for_company(
            db,
            company,
            query=query,
            limit=max(1, min(5000, pages_per_query * page_size * 2)),
            pages=max(1, min(200, pages_per_query)),
            page_size=max(10, min(50, page_size)),
            region=None,
            dedupe_mode=mode,
        )

        bulk.breakdown.append(
            EISSiteBulkQueryStats(
                query=query,
                stage=stats.stage,
                reason=stats.reason,
                source_status=stats.source_status,
                pages=stats.pages,
                candidates=stats.candidates,
                inserted=stats.inserted,
                updated=stats.updated,
                skipped=stats.skipped,
            )
        )
        bulk.totals.candidates += stats.candidates
        bulk.totals.inserted += stats.inserted
        bulk.totals.updated += stats.updated
        bulk.totals.skipped += stats.skipped
        bulk.totals.pages += stats.pages
        bulk.totals.fetched_bytes += stats.fetched_bytes
        bulk.totals.error_count += stats.error_count
        if stats.source_status == "blocked":
            bulk.blocked_count += 1
        if stats.source_status == "maintenance":
            bulk.maintenance_count += 1
        if stats.errors_sample:
            for err in stats.errors_sample:
                if len(bulk.totals.errors_sample) >= 3:
                    break
                if err not in bulk.totals.errors_sample:
                    bulk.totals.errors_sample.append(err)
        if stop_if_blocked and stats.source_status in {"blocked", "maintenance", "cooldown"}:
            bulk.totals.stage = stats.stage
            bulk.totals.reason = stats.reason
            bulk.totals.source_status = stats.source_status
            break

    bulk.totals.stage = "done"
    bulk.totals.reason = "ok"
    bulk.totals.source_status = "ok" if bulk.blocked_count == 0 and bulk.maintenance_count == 0 else "error"
    return bulk
