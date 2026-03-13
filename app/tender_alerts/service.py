from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Integer, and_, cast, exists, func, literal, not_, or_, select, union_all
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.tender_alerts.model import TenderAlertView
from app.tender_alerts.schemas import AlertCategory, AlertCounts, AlertDigestResponse, AlertTenderItem
from app.tender_analysis.model import TenderAnalysis
from app.tender_decisions.model import TenderDecision
from app.tender_tasks.model import TenderTask
from app.tenders.model import Tender


def _base_tender_condition(company_id: UUID, since: datetime | None) -> list:
    conditions = [Tender.company_id == company_id]
    if since is not None:
        conditions.append(Tender.created_at >= since)
    return conditions


def _ack_filter(company_id: UUID, user_id: UUID, category_value: str):
    return not_(
        exists(
            select(1).where(
                TenderAlertView.company_id == company_id,
                TenderAlertView.user_id == user_id,
                TenderAlertView.tender_id == Tender.id,
                TenderAlertView.category == category_value,
            )
        )
    )


def _alerts_union(company_id: UUID, user_id: UUID, since: datetime | None, include_acknowledged: bool, categories: set[str] | None):
    now = datetime.now(UTC)
    soon = now + timedelta(days=3)

    queries = []

    auto_risk_score = cast(TenderAnalysis.requirements["risk_v1"]["score_auto"].astext, Integer)
    effective_risk_score = func.coalesce(TenderDecision.risk_score, auto_risk_score)

    base_fields = [
        Tender.id.label("tender_id"),
        Tender.title.label("title"),
        Tender.submission_deadline.label("deadline_at"),
        effective_risk_score.label("risk_score"),
        TenderDecision.recommendation.label("recommendation"),
    ]

    def include(cat: str) -> bool:
        return categories is None or cat in categories

    if include(AlertCategory.NEW.value):
        where = [* _base_tender_condition(company_id, since), Tender.status == "new"]
        if not include_acknowledged:
            where.append(_ack_filter(company_id, user_id, AlertCategory.NEW.value))
        queries.append(
            select(*base_fields, literal(AlertCategory.NEW.value).label("category"))
            .select_from(Tender)
            .outerjoin(TenderDecision, and_(TenderDecision.company_id == company_id, TenderDecision.tender_id == Tender.id))
            .outerjoin(TenderAnalysis, and_(TenderAnalysis.company_id == company_id, TenderAnalysis.tender_id == Tender.id))
            .where(*where)
        )

    if include(AlertCategory.DEADLINE_SOON.value):
        where = [
            *_base_tender_condition(company_id, since),
            Tender.submission_deadline.is_not(None),
            Tender.submission_deadline >= now,
            Tender.submission_deadline <= soon,
            Tender.status.notin_(["won", "lost", "archived"]),
        ]
        if not include_acknowledged:
            where.append(_ack_filter(company_id, user_id, AlertCategory.DEADLINE_SOON.value))
        queries.append(
            select(*base_fields, literal(AlertCategory.DEADLINE_SOON.value).label("category"))
            .select_from(Tender)
            .outerjoin(TenderDecision, and_(TenderDecision.company_id == company_id, TenderDecision.tender_id == Tender.id))
            .outerjoin(TenderAnalysis, and_(TenderAnalysis.company_id == company_id, TenderAnalysis.tender_id == Tender.id))
            .where(*where)
        )

    if include(AlertCategory.RISKY.value):
        effective_recommendation_ok = or_(TenderDecision.recommendation.is_(None), TenderDecision.recommendation != "no_go")
        where = [
            *_base_tender_condition(company_id, since),
            effective_risk_score >= 70,
            effective_recommendation_ok,
        ]
        if not include_acknowledged:
            where.append(_ack_filter(company_id, user_id, AlertCategory.RISKY.value))
        queries.append(
            select(*base_fields, literal(AlertCategory.RISKY.value).label("category"))
            .select_from(Tender)
            .outerjoin(TenderDecision, and_(TenderDecision.company_id == company_id, TenderDecision.tender_id == Tender.id))
            .outerjoin(TenderAnalysis, and_(TenderAnalysis.company_id == company_id, TenderAnalysis.tender_id == Tender.id))
            .where(*where)
        )

    if include(AlertCategory.GO.value):
        where = [*_base_tender_condition(company_id, since), TenderDecision.recommendation.in_(["go", "strong_go"])]
        if not include_acknowledged:
            where.append(_ack_filter(company_id, user_id, AlertCategory.GO.value))
        queries.append(
            select(*base_fields, literal(AlertCategory.GO.value).label("category"))
            .select_from(Tender)
            .join(TenderDecision, and_(TenderDecision.company_id == company_id, TenderDecision.tender_id == Tender.id))
            .outerjoin(TenderAnalysis, and_(TenderAnalysis.company_id == company_id, TenderAnalysis.tender_id == Tender.id))
            .where(*where)
        )

    if include(AlertCategory.NO_GO.value):
        where = [*_base_tender_condition(company_id, since), TenderDecision.recommendation == "no_go"]
        if not include_acknowledged:
            where.append(_ack_filter(company_id, user_id, AlertCategory.NO_GO.value))
        queries.append(
            select(*base_fields, literal(AlertCategory.NO_GO.value).label("category"))
            .select_from(Tender)
            .join(TenderDecision, and_(TenderDecision.company_id == company_id, TenderDecision.tender_id == Tender.id))
            .outerjoin(TenderAnalysis, and_(TenderAnalysis.company_id == company_id, TenderAnalysis.tender_id == Tender.id))
            .where(*where)
        )

    if include(AlertCategory.OVERDUE_TASK.value):
        overdue_exists = exists(
            select(1).where(
                TenderTask.company_id == company_id,
                TenderTask.tender_id == Tender.id,
                TenderTask.status == "overdue",
            )
        )
        where = [*_base_tender_condition(company_id, since), overdue_exists]
        if not include_acknowledged:
            where.append(_ack_filter(company_id, user_id, AlertCategory.OVERDUE_TASK.value))
        queries.append(
            select(*base_fields, literal(AlertCategory.OVERDUE_TASK.value).label("category"))
            .select_from(Tender)
            .outerjoin(TenderDecision, and_(TenderDecision.company_id == company_id, TenderDecision.tender_id == Tender.id))
            .outerjoin(TenderAnalysis, and_(TenderAnalysis.company_id == company_id, TenderAnalysis.tender_id == Tender.id))
            .where(*where)
        )

    if not queries:
        return None

    return union_all(*queries).subquery("alerts_union")


async def build_alert_digest(
    db: AsyncSession,
    company_id: UUID,
    user_id: UUID,
    since: datetime | None,
    include_acknowledged: bool,
    categories: list[AlertCategory] | None,
    limit: int = 100,
) -> AlertDigestResponse:
    category_set = {c.value for c in categories} if categories else None
    alert_union = _alerts_union(company_id, user_id, since, include_acknowledged, category_set)

    counts = AlertCounts()
    items: list[AlertTenderItem] = []

    if alert_union is None:
        return AlertDigestResponse(counts=counts, items=items)

    counts_stmt = select(alert_union.c.category, func.count().label("cnt")).group_by(alert_union.c.category)
    count_rows = (await db.execute(counts_stmt)).all()
    for category, cnt in count_rows:
        if category in counts.model_fields:
            setattr(counts, category, int(cnt))

    items_stmt = (
        select(
            alert_union.c.tender_id,
            alert_union.c.title,
            alert_union.c.category,
            alert_union.c.deadline_at,
            alert_union.c.risk_score,
            alert_union.c.recommendation,
        )
        .order_by(
            alert_union.c.deadline_at.asc().nulls_last(),
            alert_union.c.tender_id.desc(),
        )
        .limit(min(limit, 100))
    )

    rows = (await db.execute(items_stmt)).all()
    for row in rows:
        items.append(
            AlertTenderItem(
                tender_id=row.tender_id,
                title=row.title,
                category=row.category,
                deadline_at=row.deadline_at,
                risk_score=row.risk_score,
                recommendation=row.recommendation,
            )
        )

    return AlertDigestResponse(counts=counts, items=items)


async def ack_alert(
    db: AsyncSession,
    company_id: UUID,
    user_id: UUID,
    tender_id: UUID,
    category: AlertCategory,
) -> None:
    stmt = insert(TenderAlertView).values(
        company_id=company_id,
        user_id=user_id,
        tender_id=tender_id,
        category=category.value,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["company_id", "user_id", "tender_id", "category"],
    )
    await db.execute(stmt)
    await db.commit()


async def ensure_tender_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> bool:
    tender_exists = await db.scalar(select(Tender.id).where(Tender.company_id == company_id, Tender.id == tender_id))
    return tender_exists is not None
