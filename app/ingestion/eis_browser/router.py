from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user_optional
from app.ingestion.eis_browser.service import run_eis_browser_once_for_company
from app.models import Company, User

router = APIRouter(prefix="/ingestion/eis-browser", tags=["ingestion"])


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


@router.post("/run-once")
async def run_eis_browser_once(
    q: str | None = Query(default=None),
    pages: int = Query(default=3, ge=1, le=5),
    page_size: int = Query(default=20, ge=10, le=50),
    limit: int = Query(default=50, ge=1, le=50),
    region: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(_get_ingestion_current_user),
) -> dict:
    company = await _get_company_for_user(db, current_user)
    stats = await run_eis_browser_once_for_company(
        db,
        company,
        query=q,
        pages=pages,
        page_size=page_size,
        limit=limit,
        region=region,
    )
    return {
        "stage": stats.stage,
        "reason": stats.reason,
        "source_status": stats.source_status,
        "pages": stats.pages,
        "candidates": stats.candidates,
        "inserted": stats.inserted,
        "updated": stats.updated,
        "skipped": stats.skipped,
        "error_count": stats.error_count,
        "errors_sample": stats.errors_sample,
        "last_run_at": stats.last_run_at.isoformat(),
    }


@router.get("/health")
async def get_eis_browser_health(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(_get_ingestion_current_user),
) -> dict:
    company = await _get_company_for_user(db, current_user)
    raw = company.ingestion_settings if isinstance(company.ingestion_settings, dict) else {}
    eis_browser = raw.get("eis_browser") if isinstance(raw.get("eis_browser"), dict) else {}
    state = eis_browser.get("state") if isinstance(eis_browser.get("state"), dict) else {}
    return {
        "company_id": str(company.id),
        "enabled": bool(eis_browser.get("enabled", True)),
        "state": state,
    }
