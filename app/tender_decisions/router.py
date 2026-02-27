from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.tender_decisions.schemas import (
    TenderDecisionCreate,
    TenderDecisionPatch,
    TenderDecisionRead,
    TenderDecisionRecommend,
)
from app.tender_decisions.service import (
    DecisionConflictError,
    DecisionValidationError,
    ScopedNotFoundError,
    create_decision,
    ensure_tender_scoped,
    get_decision_scoped,
    patch_decision,
    set_recommendation,
)

router = APIRouter(prefix="/tenders/{tender_id}/decision", tags=["tender-decisions"])


@router.post("", response_model=TenderDecisionRead, status_code=status.HTTP_201_CREATED)
async def create_decision_endpoint(
    tender_id: UUID,
    payload: TenderDecisionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderDecisionRead:
    try:
        decision = await create_decision(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            payload=payload,
        )
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DecisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DecisionValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return TenderDecisionRead.model_validate(decision)


@router.get("", response_model=TenderDecisionRead)
async def get_decision_endpoint(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderDecisionRead:
    try:
        await ensure_tender_scoped(db, current_user.company_id, tender_id)
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    decision = await get_decision_scoped(db, current_user.company_id, tender_id)
    if decision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Decision not found")

    return TenderDecisionRead.model_validate(decision)


@router.patch("", response_model=TenderDecisionRead)
async def patch_decision_endpoint(
    tender_id: UUID,
    payload: TenderDecisionPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderDecisionRead:
    try:
        decision = await patch_decision(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            payload=payload,
        )
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DecisionValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return TenderDecisionRead.model_validate(decision)


@router.post("/recommend", response_model=TenderDecisionRead)
async def recommend_decision_endpoint(
    tender_id: UUID,
    payload: TenderDecisionRecommend,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderDecisionRead:
    try:
        decision = await set_recommendation(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            payload=payload,
        )
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return TenderDecisionRead.model_validate(decision)
