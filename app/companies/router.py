from typing import Any
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import CompanyRead
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Company, User
from app.telegram_notify.service import mask_telegram_profile

router = APIRouter(prefix="/companies", tags=["companies"])

_TOP_LEVEL_FIELDS = {"inn", "ogrn", "name", "legal_address", "bank_details"}


class CompanyProfileResponse(BaseModel):
    profile: dict


class CompanyProfilePatchRequest(BaseModel):
    profile: dict


@router.get("/me", response_model=CompanyRead)
async def get_my_company(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompanyRead:
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    payload = CompanyRead.model_validate(company).model_dump(mode="json")
    payload["profile"] = mask_telegram_profile(payload.get("profile") or {})
    return CompanyRead.model_validate(payload)


@router.get("/me/profile", response_model=CompanyProfileResponse)
async def get_my_company_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompanyProfileResponse:
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    profile = company.profile if isinstance(company.profile, dict) else {}
    return CompanyProfileResponse(profile=mask_telegram_profile(profile))


@router.patch("/me/profile", response_model=CompanyProfileResponse)
async def patch_my_company_profile(
    payload: CompanyProfilePatchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompanyProfileResponse:
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    company.profile = payload.profile
    await db.commit()
    await db.refresh(company)

    return CompanyProfileResponse(profile=mask_telegram_profile(payload.profile))


# ─── Профиль компании: полные данные + jsonb merge ───────────────────────────

class CompanyFullProfileResponse(BaseModel):
    id: Any
    name: str | None
    inn: str | None
    ogrn: str | None
    legal_address: str | None
    bank_details: dict | None
    # profile fields (flattened)
    okved_main: str | None = None
    okved_additional: list[str] = []
    sro: dict = {}
    licenses: list[dict] = []
    experience: dict = {}
    financial: dict = {}
    regions: list[str] = []
    documents: list[dict] = []


class CompanyFullProfilePatch(BaseModel):
    # top-level columns
    name: str | None = None
    inn: str | None = None
    ogrn: str | None = None
    legal_address: str | None = None
    bank_details: dict | None = None
    # profile fields
    okved_main: str | None = None
    okved_additional: list[str] | None = None
    sro: dict | None = None
    licenses: list[dict] | None = None
    experience: dict | None = None
    financial: dict | None = None
    regions: list[str] | None = None
    documents: list[dict] | None = None


@router.get("/me/full", response_model=CompanyFullProfileResponse)
async def get_my_company_full(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompanyFullProfileResponse:
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    p = company.profile if isinstance(company.profile, dict) else {}
    return CompanyFullProfileResponse(
        id=str(company.id),
        name=company.name,
        inn=company.inn,
        ogrn=company.ogrn,
        legal_address=company.legal_address,
        bank_details=company.bank_details,
        okved_main=p.get("okved_main"),
        okved_additional=p.get("okved_additional", []),
        sro=p.get("sro", {}),
        licenses=p.get("licenses", []),
        experience=p.get("experience", {}),
        financial=p.get("financial", {}),
        regions=p.get("regions", []),
        documents=p.get("documents", []),
    )


@router.patch("/me/full", response_model=CompanyFullProfileResponse)
async def patch_my_company_full(
    payload: CompanyFullProfilePatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompanyFullProfileResponse:
    company = await db.scalar(select(Company).where(Company.id == current_user.company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    data = payload.model_dump(exclude_unset=True)

    # Write top-level columns directly
    for field in _TOP_LEVEL_FIELDS:
        if field in data:
            setattr(company, field, data.pop(field))

    # Merge remaining keys into profile jsonb
    if data:
        current_profile = company.profile if isinstance(company.profile, dict) else {}
        company.profile = {**current_profile, **data}

    from sqlalchemy.orm import attributes
    attributes.flag_modified(company, "profile")

    await db.commit()
    await db.refresh(company)

    p = company.profile if isinstance(company.profile, dict) else {}
    return CompanyFullProfileResponse(
        id=str(company.id),
        name=company.name,
        inn=company.inn,
        ogrn=company.ogrn,
        legal_address=company.legal_address,
        bank_details=company.bank_details,
        okved_main=p.get("okved_main"),
        okved_additional=p.get("okved_additional", []),
        sro=p.get("sro", {}),
        licenses=p.get("licenses", []),
        experience=p.get("experience", {}),
        financial=p.get("financial", {}),
        regions=p.get("regions", []),
        documents=p.get("documents", []),
    )
