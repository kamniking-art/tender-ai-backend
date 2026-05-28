from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Company, User
from app.monitoring.schemas import MonitoringRunResponse, MonitoringSettings, MonitoringSettingsPatch
from app.monitoring.service import get_monitoring_settings, patch_monitoring_settings, run_monitoring_cycle

settings_router = APIRouter(prefix="/companies/me/monitoring-settings", tags=["monitoring"])
router = APIRouter(prefix="/monitoring", tags=["monitoring"])


async def _get_company(db: AsyncSession, company_id) -> Company:
    company = await db.scalar(select(Company).where(Company.id == company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return company


@settings_router.get("", response_model=MonitoringSettings)
async def get_company_monitoring_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MonitoringSettings:
    company = await _get_company(db, current_user.company_id)
    return get_monitoring_settings(company)


@settings_router.patch("", response_model=MonitoringSettings)
async def update_company_monitoring_settings(
    payload: MonitoringSettingsPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MonitoringSettings:
    company = await _get_company(db, current_user.company_id)
    settings_data = patch_monitoring_settings(company, payload)
    await db.commit()
    await db.refresh(company)
    return settings_data


@router.post("/run-once", response_model=MonitoringRunResponse)
async def run_monitoring_once(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MonitoringRunResponse:
    company = await _get_company(db, current_user.company_id)
    return await run_monitoring_cycle(db, company=company, actor_user_id=current_user.id)


async def _query_view(db: AsyncSession, view_name: str) -> list[dict[str, Any]]:
    """Execute SELECT * FROM <view> and return rows as plain dicts."""
    result = await db.execute(text(f"SELECT * FROM {view_name}"))  # noqa: S608
    keys = list(result.keys())
    return [dict(zip(keys, row)) for row in result.fetchall()]


@router.get("/operational")
async def get_operational_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return aggregated operational metrics from the three monitoring views.

    Reads v_cost_by_tenant, v_provider_errors, v_health_per_tenant and
    returns them as JSON arrays. Requires a valid auth token.
    """
    try:
        cost_by_tenant = await _query_view(db, "v_cost_by_tenant")
    except Exception:
        cost_by_tenant = []

    try:
        provider_errors = await _query_view(db, "v_provider_errors")
    except Exception:
        provider_errors = []

    try:
        health_per_tenant = await _query_view(db, "v_health_per_tenant")
    except Exception:
        health_per_tenant = []

    try:
        queue_backlog = await _query_view(db, "v_queue_backlog")
    except Exception:
        queue_backlog = []

    return {
        "cost_by_tenant": cost_by_tenant,
        "provider_errors": provider_errors,
        "health_per_tenant": health_per_tenant,
        "queue_backlog": queue_backlog,
    }

