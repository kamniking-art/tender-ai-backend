from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user_optional
from app.ingestion.eis_site.service import run_eis_site_bulk_for_company, run_eis_site_once_for_company
from app.models import Company, User

router = APIRouter(prefix="/ingestion/eis-site", tags=["ingestion"])

_RUN_ONCE_GUARD_LOCK = asyncio.Lock()
_RUN_ONCE_LAST_CALLED_AT: dict[str, float] = {}


async def _get_ingestion_current_user(current_user: User | None = Depends(get_current_user_optional)) -> User | None:
    if settings.auth_disabled_enabled:
        return current_user
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


async def _get_company_for_user(db: AsyncSession, current_user: User | None) -> Company:
    company: Company | None
    if current_user is None:
        company_id = await db.scalar(select(User.company_id).where(User.email == settings.auth_disabled_company_email).limit(1))
        if company_id is not None:
            company = await db.scalar(select(Company).where(Company.id == company_id))
        else:
            company = await db.scalar(select(Company).order_by(Company.created_at.asc()))
    else:
        company = await db.scalar(select(Company).where(Company.id == current_user.company_id))

    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return company


async def _enforce_run_once_rate_limit(company: Company) -> None:
    if not settings.auth_disabled_enabled:
        return

    cooldown_seconds = max(60, settings.ingestion_run_once_cooldown_minutes * 60)
    now = time.monotonic()
    key = str(company.id)

    async with _RUN_ONCE_GUARD_LOCK:
        last_called_at = _RUN_ONCE_LAST_CALLED_AT.get(key)
        if last_called_at is not None and now - last_called_at < cooldown_seconds:
            retry_after = int(cooldown_seconds - (now - last_called_at))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Run-once rate limit is active. Retry in {retry_after} seconds.",
            )
        _RUN_ONCE_LAST_CALLED_AT[key] = now


@router.post("/run-once")
async def run_eis_site_once(
    q: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    pages: int = Query(default=50, ge=1, le=200),
    page_size: int = Query(default=20, ge=10, le=50),
    region: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(_get_ingestion_current_user),
) -> dict:
    company = await _get_company_for_user(db, current_user)
    await _enforce_run_once_rate_limit(company)

    stats = await run_eis_site_once_for_company(
        db,
        company,
        query=q,
        limit=limit,
        pages=pages,
        page_size=page_size,
        region=region,
    )
    return {
        "stage": stats.stage,
        "reason": stats.reason,
        "source_status": stats.source_status,
        "cooldown_until": stats.cooldown_until.isoformat() if stats.cooldown_until else None,
        "http_status": stats.http_status,
        "fetched_bytes": stats.fetched_bytes,
        "error_count": stats.error_count,
        "errors_sample": stats.errors_sample,
        "pages": stats.pages,
        "candidates": stats.candidates,
        "inserted": stats.inserted,
        "updated": stats.updated,
        "skipped": stats.skipped,
    }


@router.post("/run-bulk")
async def run_eis_site_bulk(
    queries: list[str] | None = Query(default=None),
    pages_per_query: int = Query(default=10, ge=1, le=200),
    page_size: int = Query(default=20, ge=10, le=50),
    dedupe_mode: str = Query(default="update"),
    stop_if_blocked: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(_get_ingestion_current_user),
) -> dict:
    company = await _get_company_for_user(db, current_user)
    await _enforce_run_once_rate_limit(company)

    bulk = await run_eis_site_bulk_for_company(
        db,
        company,
        queries=queries or settings.eis_site_queries_list,
        pages_per_query=pages_per_query,
        page_size=page_size,
        dedupe_mode=dedupe_mode,
        stop_if_blocked=stop_if_blocked,
    )
    return {
        "stage": bulk.totals.stage,
        "reason": bulk.totals.reason,
        "source_status": bulk.totals.source_status,
        "totals": {
            "pages": bulk.totals.pages,
            "candidates": bulk.totals.candidates,
            "inserted": bulk.totals.inserted,
            "updated": bulk.totals.updated,
            "skipped": bulk.totals.skipped,
            "fetched_bytes": bulk.totals.fetched_bytes,
            "error_count": bulk.totals.error_count,
            "errors_sample": bulk.totals.errors_sample,
        },
        "blocked_count": bulk.blocked_count,
        "maintenance_count": bulk.maintenance_count,
        "breakdown": [
            {
                "query": item.query,
                "stage": item.stage,
                "reason": item.reason,
                "source_status": item.source_status,
                "pages": item.pages,
                "candidates": item.candidates,
                "inserted": item.inserted,
                "updated": item.updated,
                "skipped": item.skipped,
            }
            for item in bulk.breakdown
        ],
    }


@router.get("/health")
async def get_eis_site_health(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(_get_ingestion_current_user),
) -> dict:
    company = await _get_company_for_user(db, current_user)
    raw = company.ingestion_settings if isinstance(company.ingestion_settings, dict) else {}
    eis_site = raw.get("eis_site") if isinstance(raw.get("eis_site"), dict) else {}
    state = eis_site.get("state") if isinstance(eis_site.get("state"), dict) else {}
    source = state.get("source") if isinstance(state.get("source"), dict) else {}
    return {
        "company_id": str(company.id),
        "enabled": bool(eis_site.get("enabled", True)),
        "query": eis_site.get("query", ""),
        "limit": int(eis_site.get("limit", 1000) or 1000),
        "pages": int(eis_site.get("max_pages", 50) or 50),
        "page_size": int(eis_site.get("records_per_page", 20) or 20),
        "region": eis_site.get("region"),
        "source_status": source.get("source_status", "ok"),
        "last_block_time": source.get("last_block_time"),
        "cooldown_until": source.get("cooldown_until"),
    }
