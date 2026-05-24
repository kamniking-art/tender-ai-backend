from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.agent_eval.service import (
    AgentEvaluation,
    get_evaluation,
    get_evaluation_stats,
    upsert_evaluation,
)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


# ── Response schemas ───────────────────────────────────────────────────────────


class AgentEvaluationResponse(BaseModel):
    id: UUID
    company_id: UUID
    tender_id: UUID
    agent_recommendation: str | None
    human_decision: str | None
    actual_result: str | None
    was_right: bool | None
    notes: str | None
    evaluated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EvaluationStatsResponse(BaseModel):
    total: int
    conclusive: int
    correct: int
    incorrect: int
    accuracy_pct: float | None


# ── Request body ───────────────────────────────────────────────────────────────


class UpsertEvaluationRequest(BaseModel):
    agent_recommendation: str | None = None
    human_decision: str | None = None
    actual_result: str | None = None
    notes: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/tender/{tender_id}",
    response_model=AgentEvaluationResponse,
    status_code=status.HTTP_200_OK,
)
async def upsert_evaluation_endpoint(
    tender_id: UUID,
    payload: UpsertEvaluationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AgentEvaluationResponse:
    """Create or update an agent evaluation record for a tender."""
    evaluation = await upsert_evaluation(
        db,
        company_id=current_user.company_id,
        tender_id=tender_id,
        agent_recommendation=payload.agent_recommendation,
        human_decision=payload.human_decision,
        actual_result=payload.actual_result,
        notes=payload.notes,
    )
    return AgentEvaluationResponse.model_validate(evaluation)


@router.get("/tender/{tender_id}", response_model=AgentEvaluationResponse)
async def get_evaluation_endpoint(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AgentEvaluationResponse:
    """Get the evaluation record for a specific tender."""
    evaluation = await get_evaluation(
        db,
        company_id=current_user.company_id,
        tender_id=tender_id,
    )
    if evaluation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evaluation for tender {tender_id} not found",
        )
    return AgentEvaluationResponse.model_validate(evaluation)


@router.get("/stats", response_model=EvaluationStatsResponse)
async def get_stats_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EvaluationStatsResponse:
    """Get aggregate agent accuracy statistics for the current company."""
    stats = await get_evaluation_stats(db, company_id=current_user.company_id)
    return EvaluationStatsResponse(**stats)
