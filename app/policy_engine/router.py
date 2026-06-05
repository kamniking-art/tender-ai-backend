from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.policy_engine.loader import Policy

router = APIRouter(prefix="/policies", tags=["policies"])


class PolicyListItem(BaseModel):
    policy_id: UUID
    policy_type: str
    active: bool
    priority: int
    action_type: str
    description: str


class PolicyToggleResponse(BaseModel):
    policy_id: UUID
    active: bool


class ApplyTemplateRequest(BaseModel):
    template_key: str


class ApplyTemplateResponse(BaseModel):
    template: str
    template_name: str
    inserted: list[str]
    skipped_existing: list[str]


@router.get("", response_model=list[PolicyListItem])
async def list_policies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PolicyListItem]:
    rows = await db.scalars(
        select(Policy)
        .where(Policy.company_id == current_user.company_id)
        .order_by(Policy.priority.desc(), Policy.created_at.asc())
    )

    items: list[PolicyListItem] = []
    for row in rows:
        action: dict = row.action if isinstance(row.action, dict) else {}
        payload: dict = action.get("payload", {})
        description = payload.get("message") or payload.get("reason") or ""
        items.append(PolicyListItem(
            policy_id=row.policy_id,
            policy_type=row.policy_type,
            active=row.active,
            priority=row.priority,
            action_type=action.get("type", ""),
            description=description,
        ))
    return items


@router.patch("/{policy_id}/toggle", response_model=PolicyToggleResponse)
async def toggle_policy(
    policy_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PolicyToggleResponse:
    policy = await db.scalar(
        select(Policy).where(
            Policy.policy_id == policy_id,
            Policy.company_id == current_user.company_id,
        )
    )
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")

    policy.active = not policy.active
    attributes.flag_modified(policy, "active")
    await db.commit()
    await db.refresh(policy)

    return PolicyToggleResponse(policy_id=policy.policy_id, active=policy.active)


@router.post("/apply-template", response_model=ApplyTemplateResponse)
async def apply_template_endpoint(
    body: ApplyTemplateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApplyTemplateResponse:
    from app.policy_engine.templates import apply_template, POLICY_TEMPLATES
    if body.template_key not in POLICY_TEMPLATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown template '{body.template_key}'. Available: {list(POLICY_TEMPLATES)}",
        )
    result = await apply_template(db, current_user.company_id, body.template_key)
    return ApplyTemplateResponse(**result)
