from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.ingestion.eis_opendata.schemas import EISOpenDataSettings, IngestionSettingsPatch
from app.ingestion.eis_opendata.service import list_available_datasets, run_eis_opendata_once_for_company
from app.ingestion.scheduler import scheduler as ingestion_scheduler
from app.models import Company, User

settings_router = APIRouter(prefix="/companies/me/ingestion-settings", tags=["ingestion"])
opendata_router = APIRouter(prefix="/ingestion/eis-opendata", tags=["ingestion"])
health_router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@settings_router.get("")
async def get_ingestion_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    company = await _get_company_for_user(db, current_user)
    return company.ingestion_settings or {}


@settings_router.patch("")
async def patch_ingestion_settings(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Payload must be JSON object")

    company = await _get_company_for_user(db, current_user)

    validated_patch = _validate_patch_payload(payload)
    current = dict(company.ingestion_settings or {})
    current.update(validated_patch)

    company.ingestion_settings = current
    await db.commit()
    await db.refresh(company)
    return company.ingestion_settings


@opendata_router.get("/datasets")
async def get_eis_opendata_datasets(
    q: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    company = await _get_company_for_user(db, current_user)
    settings = _extract_opendata_settings(company.ingestion_settings or {})
    datasets = await list_available_datasets(settings=settings, q=q, limit=limit)
    return [item.model_dump(mode="json") for item in datasets]


@opendata_router.post("/run-once")
async def run_eis_opendata_once(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    company = await _get_company_for_user(db, current_user)
    stats = await run_eis_opendata_once_for_company(db, company)
    return {
        "datasets": stats.datasets_count,
        "files": stats.files_count,
        "candidates": stats.candidates_count,
        "inserted": stats.inserted_count,
        "updated": stats.updated_count,
        "skipped": stats.skipped_count,
    }


@health_router.get("/health")
async def get_ingestion_health(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    company = await _get_company_for_user(db, current_user)
    settings = company.ingestion_settings or {}

    eis_public = settings.get("eis_public") if isinstance(settings.get("eis_public"), dict) else {}
    eis_public_state = eis_public.get("state") if isinstance(eis_public.get("state"), dict) else {}

    eis_opendata = settings.get("eis_opendata") if isinstance(settings.get("eis_opendata"), dict) else {}
    od_state = eis_opendata.get("state") if isinstance(eis_opendata.get("state"), dict) else {}
    od_discovery = od_state.get("discovery") if isinstance(od_state.get("discovery"), dict) else {}

    snapshot = ingestion_scheduler.get_health_snapshot()
    return {
        "company_id": str(company.id),
        "eis_public": {
            "enabled": bool(eis_public.get("enabled", False)),
            "cooldown_until": eis_public_state.get("cooldown_until"),
        },
        "eis_opendata": {
            "enabled": bool(eis_opendata.get("enabled", False)),
            "discovery": {
                "status": od_discovery.get("status", "unknown"),
                "cooldown_until": od_discovery.get("cooldown_until"),
                "last_success_at": od_discovery.get("last_success_at"),
                "search_api_url": od_discovery.get("search_api_url"),
                "dataset_api_url": od_discovery.get("dataset_api_url"),
                "last_error": od_discovery.get("last_error"),
            },
        },
        "scheduler": snapshot,
    }


async def _get_company_for_user(db: AsyncSession, current_user: User) -> Company:
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return company


def _extract_opendata_settings(settings: dict) -> EISOpenDataSettings:
    raw = settings.get("eis_opendata") if isinstance(settings.get("eis_opendata"), dict) else {}
    return EISOpenDataSettings.model_validate(raw)


def _validate_patch_payload(payload: dict) -> dict:
    model = IngestionSettingsPatch.model_validate(payload)
    result: dict = {}

    if model.eis_public is not None:
        result["eis_public"] = model.eis_public

    if model.eis_opendata is not None:
        result["eis_opendata"] = model.eis_opendata.model_dump(mode="json")

    passthrough_keys = set(payload.keys()) - {"eis_public", "eis_opendata"}
    for key in passthrough_keys:
        result[key] = payload[key]

    return result
