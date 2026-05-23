from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.clarification.schema import ClarificationStateError
from app.clarification.service import (
    ClarificationQuestion,
    approve_question,
    create_question,
    list_questions,
    mark_sent,
    record_answer,
    timeout_question,
    _get_question,
)

router = APIRouter(prefix="/clarification", tags=["clarification"])


# ── Response schema ────────────────────────────────────────────────────────────


class ClarificationQuestionResponse(BaseModel):
    id: UUID
    company_id: UUID
    tender_id: UUID
    question_text: str
    reason: str | None
    status: str
    sent_at: datetime | None
    answer_text: str | None
    answered_at: datetime | None
    timeout_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ── Request bodies ─────────────────────────────────────────────────────────────


class CreateQuestionRequest(BaseModel):
    tender_id: UUID
    question_text: str
    reason: str | None = None
    timeout_at: datetime | None = None


class RecordAnswerRequest(BaseModel):
    answer_text: str


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("", response_model=ClarificationQuestionResponse, status_code=status.HTTP_201_CREATED)
async def create_question_endpoint(
    payload: CreateQuestionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClarificationQuestionResponse:
    """Create a new clarification question (starts in 'draft' status)."""
    q = await create_question(
        db,
        company_id=current_user.company_id,
        tender_id=payload.tender_id,
        question_text=payload.question_text,
        reason=payload.reason,
        timeout_at=payload.timeout_at,
    )
    return ClarificationQuestionResponse.model_validate(q)


@router.get("/tender/{tender_id}", response_model=list[ClarificationQuestionResponse])
async def list_questions_endpoint(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ClarificationQuestionResponse]:
    """List all clarification questions for a tender (newest first)."""
    questions = await list_questions(
        db,
        company_id=current_user.company_id,
        tender_id=tender_id,
    )
    return [ClarificationQuestionResponse.model_validate(q) for q in questions]


@router.get("/{question_id}", response_model=ClarificationQuestionResponse)
async def get_question_endpoint(
    question_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClarificationQuestionResponse:
    """Get a single clarification question by ID."""
    try:
        q = await _get_question(db, question_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if q.company_id != current_user.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return ClarificationQuestionResponse.model_validate(q)


@router.post("/{question_id}/approve", response_model=ClarificationQuestionResponse)
async def approve_question_endpoint(
    question_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClarificationQuestionResponse:
    """Approve a draft question (draft → approved)."""
    try:
        q = await approve_question(db, question_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ClarificationStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if q.company_id != current_user.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return ClarificationQuestionResponse.model_validate(q)


@router.post("/{question_id}/mark_sent", response_model=ClarificationQuestionResponse)
async def mark_sent_endpoint(
    question_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClarificationQuestionResponse:
    """Mark an approved question as sent (approved → sent)."""
    try:
        q = await mark_sent(db, question_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ClarificationStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if q.company_id != current_user.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return ClarificationQuestionResponse.model_validate(q)


@router.post("/{question_id}/answer", response_model=ClarificationQuestionResponse)
async def record_answer_endpoint(
    question_id: UUID,
    payload: RecordAnswerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClarificationQuestionResponse:
    """Record a received answer (sent → answered)."""
    try:
        q = await record_answer(db, question_id, payload.answer_text)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ClarificationStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if q.company_id != current_user.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return ClarificationQuestionResponse.model_validate(q)
