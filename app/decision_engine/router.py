from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.decision_engine.schemas import DecisionEngineReadResponse, DecisionRecomputeRequest, DecisionRecomputeResponse
from app.decision_engine.service import (
    DecisionEngineBadRequestError,
    ManualRecommendationConflictError,
    get_decision_engine_scoped,
    recompute_decision_engine_v1,
)
from app.models import User

router = APIRouter(prefix="/tenders/{tender_id}/decision", tags=["decision-engine"])


@router.post("/recompute", response_model=DecisionRecomputeResponse)
async def recompute_decision(
    tender_id: UUID,
    payload: DecisionRecomputeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DecisionRecomputeResponse:
    try:
        decision, engine = await recompute_decision_engine_v1(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            force=payload.force,
        )
    except ManualRecommendationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DecisionEngineBadRequestError as exc:
        detail = str(exc)
        code = status.HTTP_404_NOT_FOUND if detail == "Tender not found" else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail=detail) from exc

    return DecisionRecomputeResponse(recommendation=decision.recommendation, decision_engine_v1=engine)


@router.get("/engine", response_model=DecisionEngineReadResponse)
async def get_decision_engine(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DecisionEngineReadResponse:
    try:
        decision, engine_meta = await get_decision_engine_scoped(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
        )
    except DecisionEngineBadRequestError as exc:
        detail = str(exc)
        code = status.HTTP_404_NOT_FOUND if detail == "Tender not found" else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail=detail) from exc

    return DecisionEngineReadResponse(recommendation=decision.recommendation, decision_engine_v1=engine_meta)
