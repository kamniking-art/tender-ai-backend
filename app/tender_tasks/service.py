from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tender_tasks.model import TenderTask
from app.tender_tasks.schemas import TaskOrderBy, TaskStatus, TaskType, TenderTaskCreate, TenderTaskUpdate
from app.tenders.service import get_tender_by_id_scoped


class ScopedNotFoundError(Exception):
    pass


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def ensure_tender_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID):
    tender = await get_tender_by_id_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")
    return tender


async def create_task(db: AsyncSession, company_id: UUID, tender_id: UUID, user_id: UUID, data: TenderTaskCreate) -> TenderTask:
    await ensure_tender_scoped(db, company_id, tender_id)

    task = TenderTask(
        company_id=company_id,
        tender_id=tender_id,
        type=data.type,
        title=data.title,
        description=data.description,
        due_at=_to_utc(data.due_at),
        status="pending",
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def list_tasks(
    db: AsyncSession,
    company_id: UUID,
    tender_id: UUID,
    *,
    status: TaskStatus | None = None,
    type: TaskType | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    order_by: TaskOrderBy = "due_at asc",
) -> list[TenderTask]:
    await ensure_tender_scoped(db, company_id, tender_id)

    stmt = select(TenderTask).where(TenderTask.company_id == company_id, TenderTask.tender_id == tender_id)
    if status is not None:
        stmt = stmt.where(TenderTask.status == status)
    if type is not None:
        stmt = stmt.where(TenderTask.type == type)
    if due_from is not None:
        stmt = stmt.where(TenderTask.due_at >= _to_utc(due_from))
    if due_to is not None:
        stmt = stmt.where(TenderTask.due_at <= _to_utc(due_to))

    if order_by == "due_at desc":
        stmt = stmt.order_by(TenderTask.due_at.desc())
    else:
        stmt = stmt.order_by(TenderTask.due_at.asc())

    return list((await db.scalars(stmt)).all())


async def get_task_scoped(db: AsyncSession, company_id: UUID, task_id: UUID) -> TenderTask | None:
    stmt = select(TenderTask).where(TenderTask.id == task_id, TenderTask.company_id == company_id)
    return await db.scalar(stmt)


async def update_task(db: AsyncSession, company_id: UUID, task_id: UUID, user_id: UUID, data: TenderTaskUpdate) -> TenderTask:
    task = await get_task_scoped(db, company_id, task_id)
    if task is None:
        raise ScopedNotFoundError("Task not found")

    updates = data.model_dump(exclude_unset=True)
    if "due_at" in updates and updates["due_at"] is not None:
        updates["due_at"] = _to_utc(updates["due_at"])

    for field, value in updates.items():
        setattr(task, field, value)
    task.updated_by = user_id

    await db.commit()
    await db.refresh(task)
    return task


async def delete_task(db: AsyncSession, company_id: UUID, task_id: UUID) -> bool:
    task = await get_task_scoped(db, company_id, task_id)
    if task is None:
        raise ScopedNotFoundError("Task not found")

    await db.delete(task)
    await db.commit()
    return True


async def mark_overdue_tasks(db: AsyncSession) -> list[tuple[UUID, UUID]]:
    now = datetime.now(UTC)
    stmt = select(TenderTask).where(TenderTask.status == "pending", TenderTask.due_at <= now)
    tasks = list((await db.scalars(stmt)).all())

    affected: list[tuple[UUID, UUID]] = []
    for task in tasks:
        task.status = "overdue"
        affected.append((task.id, task.tender_id))

    if tasks:
        await db.commit()

    return affected
