from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.tender_tasks.schemas import TaskOrderBy, TaskStatus, TaskType, TenderTaskCreate, TenderTaskRead, TenderTaskUpdate
from app.tender_tasks.service import ScopedNotFoundError, create_task, delete_task, get_task_scoped, list_tasks, update_task

router = APIRouter(tags=["tender-tasks"])


@router.post("/tenders/{tender_id}/tasks", response_model=TenderTaskRead, status_code=status.HTTP_201_CREATED)
async def create_tender_task(
    tender_id: UUID,
    payload: TenderTaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderTaskRead:
    try:
        task = await create_task(db, current_user.company_id, tender_id, current_user.id, payload)
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return TenderTaskRead.model_validate(task)


@router.get("/tenders/{tender_id}/tasks", response_model=list[TenderTaskRead])
async def list_tender_tasks(
    tender_id: UUID,
    status: TaskStatus | None = None,
    type: TaskType | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    order_by: TaskOrderBy = "due_at asc",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TenderTaskRead]:
    try:
        tasks = await list_tasks(
            db,
            current_user.company_id,
            tender_id,
            status=status,
            type=type,
            due_from=due_from,
            due_to=due_to,
            order_by=order_by,
        )
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return [TenderTaskRead.model_validate(task) for task in tasks]


@router.get("/tender-tasks/{task_id}", response_model=TenderTaskRead)
async def get_tender_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderTaskRead:
    task = await get_task_scoped(db, current_user.company_id, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return TenderTaskRead.model_validate(task)


@router.patch("/tender-tasks/{task_id}", response_model=TenderTaskRead)
async def patch_tender_task(
    task_id: UUID,
    payload: TenderTaskUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderTaskRead:
    try:
        task = await update_task(db, current_user.company_id, task_id, current_user.id, payload)
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return TenderTaskRead.model_validate(task)


@router.delete("/tender-tasks/{task_id}")
async def delete_tender_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, bool]:
    try:
        await delete_task(db, current_user.company_id, task_id)
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"ok": True}
