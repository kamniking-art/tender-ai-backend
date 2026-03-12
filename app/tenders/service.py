from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tenders.model import Tender
from app.tenders.schemas import SortField, SortOrder, TenderCreate, TenderStatus, TenderUpdate


def _apply_filters(
    stmt: Select,
    *,
    company_id: UUID,
    status: TenderStatus | None = None,
    region: str | None = None,
    procurement_type: str | None = None,
    nmck_min: Decimal | None = None,
    nmck_max: Decimal | None = None,
    deadline_from: datetime | None = None,
    deadline_to: datetime | None = None,
    q: str | None = None,
) -> Select:
    stmt = stmt.where(Tender.company_id == company_id)

    if status is not None:
        stmt = stmt.where(Tender.status == status.value)
    if region is not None:
        stmt = stmt.where(Tender.region == region)
    if procurement_type is not None:
        stmt = stmt.where(Tender.procurement_type == procurement_type)
    if nmck_min is not None:
        stmt = stmt.where(Tender.nmck >= nmck_min)
    if nmck_max is not None:
        stmt = stmt.where(Tender.nmck <= nmck_max)
    if deadline_from is not None:
        stmt = stmt.where(Tender.submission_deadline >= deadline_from)
    if deadline_to is not None:
        stmt = stmt.where(Tender.submission_deadline <= deadline_to)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Tender.title.ilike(pattern),
                Tender.customer_name.ilike(pattern),
                Tender.external_id.ilike(pattern),
                Tender.region.ilike(pattern),
                Tender.place_text.ilike(pattern),
            )
        )

    return stmt


async def create_tender(db: AsyncSession, company_id: UUID, payload: TenderCreate) -> Tender:
    tender = Tender(
        company_id=company_id,
        source=payload.source,
        external_id=payload.external_id,
        title=payload.title,
        customer_name=payload.customer_name,
        region=payload.region,
        place_text=payload.place_text,
        procurement_type=payload.procurement_type,
        nmck=payload.nmck,
        published_at=payload.published_at,
        submission_deadline=payload.submission_deadline,
        status=payload.status.value,
    )
    db.add(tender)
    await db.commit()
    await db.refresh(tender)
    return tender


async def list_tenders(
    db: AsyncSession,
    *,
    company_id: UUID,
    status: TenderStatus | None = None,
    region: str | None = None,
    procurement_type: str | None = None,
    nmck_min: Decimal | None = None,
    nmck_max: Decimal | None = None,
    deadline_from: datetime | None = None,
    deadline_to: datetime | None = None,
    q: str | None = None,
    sort: SortField | None = None,
    order: SortOrder = SortOrder.ASC,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Tender], int]:
    base_stmt = _apply_filters(
        select(Tender),
        company_id=company_id,
        status=status,
        region=region,
        procurement_type=procurement_type,
        nmck_min=nmck_min,
        nmck_max=nmck_max,
        deadline_from=deadline_from,
        deadline_to=deadline_to,
        q=q,
    )

    count_stmt = _apply_filters(
        select(func.count()).select_from(Tender),
        company_id=company_id,
        status=status,
        region=region,
        procurement_type=procurement_type,
        nmck_min=nmck_min,
        nmck_max=nmck_max,
        deadline_from=deadline_from,
        deadline_to=deadline_to,
        q=q,
    )

    sort_mapping = {
        SortField.DEADLINE: Tender.submission_deadline,
        SortField.PUBLISHED: Tender.published_at,
        SortField.NMCK: Tender.nmck,
        SortField.CREATED: Tender.created_at,
    }

    if sort is None:
        base_stmt = base_stmt.order_by(Tender.submission_deadline.asc().nullslast(), Tender.created_at.desc())
    else:
        sort_col = sort_mapping[sort]
        direction = sort_col.asc() if order == SortOrder.ASC else sort_col.desc()
        base_stmt = base_stmt.order_by(direction.nullslast(), Tender.created_at.desc())

    base_stmt = base_stmt.limit(limit).offset(offset)

    total = int((await db.execute(count_stmt)).scalar_one())
    items = list((await db.scalars(base_stmt)).all())
    return items, total


async def get_tender_by_id_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> Tender | None:
    stmt = select(Tender).where(Tender.id == tender_id, Tender.company_id == company_id)
    return await db.scalar(stmt)


async def update_tender(db: AsyncSession, tender: Tender, payload: TenderUpdate) -> Tender:
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(tender, field, value)

    await db.commit()
    await db.refresh(tender)
    return tender


async def set_tender_status(db: AsyncSession, tender: Tender, status: TenderStatus) -> Tender:
    tender.status = status.value
    await db.commit()
    await db.refresh(tender)
    return tender
