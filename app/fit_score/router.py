from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.fit_score.schema import FitScoreComponents, FitScoreResult
from app.fit_score.service import CompanyFitScore, upsert_fit_score
from app.models import Company, User

router = APIRouter(prefix="/fit-score", tags=["fit-score"])


class FitScoreResponse(BaseModel):
    tender_id: UUID
    company_id: UUID
    okved_match: bool | None
    sro_ok: bool | None
    license_ok: bool | None
    experience_ok: bool | None
    funds_ok: bool | None
    fit_score: float | None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


@router.post("/tenders/{tender_id}/calculate", response_model=FitScoreResponse)
async def calculate_fit_score(
    tender_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FitScoreResponse:
    """Recalculate and persist the fit score for one tender."""
    from app.ai_extraction.schemas import ExtractedTenderV1
    from app.fit_score.scorer import FitScorer
    from app.requirements.normalizer import RequirementNormalizer
    from app.tender_analysis.model import TenderAnalysis
    from app.tenders.service import get_tender_by_id_scoped

    # 1. Verify tender belongs to company
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    # 2. Load company profile
    company = await db.scalar(
        select(Company).where(Company.id == current_user.company_id)
    )
    profile: dict = company.profile if company and isinstance(company.profile, dict) else {}

    # 3. Load extracted data
    analysis = await db.scalar(
        select(TenderAnalysis).where(
            TenderAnalysis.company_id == current_user.company_id,
            TenderAnalysis.tender_id == tender_id,
        )
    )
    extracted_raw = (analysis.requirements or {}).get("extracted_v1") if analysis else None
    if extracted_raw is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No extracted data found — run AI extraction first",
        )
    extracted = ExtractedTenderV1.model_validate(extracted_raw)

    # 4. Normalise requirements and score
    checklist = RequirementNormalizer().normalize(extracted)
    result = FitScorer().score(profile, checklist, extracted)

    # 5. Persist and return
    record = await upsert_fit_score(db, tender_id, current_user.company_id, result)
    return FitScoreResponse.model_validate(record)
