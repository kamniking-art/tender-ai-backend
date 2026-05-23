from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deadline_control.calculator import calculate_status
from app.deadline_control.model import DeadlineControl
from app.tenders.model import Tender

# Re-export so callers can do: from app.deadline_control.service import calculate_status
__all__ = ["calculate_status", "upsert_deadline_control", "refresh_all"]


async def upsert_deadline_control(
    db: AsyncSession,
    tender_id: UUID,
    company_id: UUID,
    submission_deadline: datetime | None,
) -> DeadlineControl:
    """Create or update a DeadlineControl record for the given tender.

    Calculates hours_remaining and deadline_status via calculate_status().
    Always sets updated_at = now().
    """
    calc = calculate_status(submission_deadline)
    now = datetime.now(timezone.utc)

    existing = await db.scalar(
        select(DeadlineControl).where(DeadlineControl.tender_id == tender_id)
    )

    if existing is not None:
        existing.submission_deadline = submission_deadline
        existing.hours_remaining = calc["hours_remaining"]
        existing.deadline_status = calc["deadline_status"]
        existing.can_recommend_go = calc["can_recommend_go"]
        existing.updated_at = now
        await db.commit()
        await db.refresh(existing)
        return existing

    record = DeadlineControl(
        tender_id=tender_id,
        company_id=company_id,
        submission_deadline=submission_deadline,
        hours_remaining=calc["hours_remaining"],
        deadline_status=calc["deadline_status"],
        can_recommend_go=calc["can_recommend_go"],
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def refresh_all(db: AsyncSession, company_id: UUID) -> int:
    """Recalculate deadline_control for all active tenders of the company.

    Skips tenders with status 'won' or 'lost' (terminal / closed states).
    Returns the number of records upserted.

    Safe to call on demand; will also be used by APScheduler in Step 8.
    """
    tenders = list(
        (
            await db.scalars(
                select(Tender).where(
                    Tender.company_id == company_id,
                    Tender.status.notin_(["won", "lost"]),
                )
            )
        ).all()
    )

    count = 0
    for tender in tenders:
        await upsert_deadline_control(
            db,
            tender_id=tender.id,
            company_id=company_id,
            submission_deadline=tender.submission_deadline,
        )
        count += 1

    return count
