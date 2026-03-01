from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_extraction.schemas import ExtractedTenderV1
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.risk.schemas import RiskRecomputeRequest, RiskRecomputeResponse
from app.risk.service import compute_risk_flags, compute_risk_score_v1
from app.tender_analysis.model import TenderAnalysis
from app.tenders.service import get_tender_by_id_scoped

router = APIRouter(prefix="/tenders/{tender_id}/risk", tags=["risk"])


@router.post("/recompute", response_model=RiskRecomputeResponse)
async def recompute_risk(
    tender_id: UUID,
    payload: RiskRecomputeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RiskRecomputeResponse:
    if not payload.use_latest_extracted:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Only use_latest_extracted=true is supported in v1")

    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    analysis = await db.scalar(
        select(TenderAnalysis).where(
            TenderAnalysis.company_id == current_user.company_id,
            TenderAnalysis.tender_id == tender_id,
        )
    )
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Analysis not found")

    if analysis.status == "approved":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approved analysis cannot be overwritten")

    extracted_payload = (analysis.requirements or {}).get("extracted_v1")
    if extracted_payload is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="extracted_v1 not found")

    try:
        extracted = ExtractedTenderV1.model_validate(extracted_payload)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid extracted_v1 payload") from exc

    risk_flags = compute_risk_flags(extracted, tender)
    risk_v1 = compute_risk_score_v1(extracted, tender)

    req = dict(analysis.requirements or {})
    req["risk_v1"] = risk_v1
    analysis.requirements = req
    analysis.risk_flags = risk_flags
    analysis.updated_by = current_user.id
    if analysis.status == "draft":
        analysis.status = "ready"

    await db.commit()

    return RiskRecomputeResponse(risk_v1=risk_v1, risk_flags=risk_flags)
