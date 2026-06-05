"""Evaluation dataset service — управление набором тендеров для оценки агента."""
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.eval_dataset.model import TenderEvalDataset
from app.agent_eval.service import get_evaluation_stats


async def add_to_dataset(
    db: AsyncSession,
    tender_id: UUID,
    expected_decision: str,
    company_id: UUID | None = None,
    expected_risks: list | None = None,
    expected_reason: str | None = None,
    verified_by: str | None = None,
    notes: str | None = None,
) -> TenderEvalDataset:
    """Добавить тендер в eval dataset."""
    import uuid
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    entry = TenderEvalDataset(
        id=uuid.uuid4(),
        tender_id=tender_id,
        company_id=company_id,
        expected_decision=expected_decision,
        expected_risks=expected_risks or [],
        expected_reason=expected_reason,
        verified_by=verified_by,
        notes=notes,
        created_at=now,
        updated_at=now,
    )
    db.add(entry)
    await db.commit()
    return entry


async def run_evaluation(db: AsyncSession, company_id: UUID | None = None) -> dict:
    """Прогнать pipeline на eval dataset и вернуть метрики."""
    from app.decision_engine.service import recompute_decision_engine_v1
    from app.tenders.model import Tender
    from app.models.user import User
    from sqlalchemy import select

    q = select(TenderEvalDataset)
    if company_id:
        q = q.where(TenderEvalDataset.company_id == company_id)
    entries = list((await db.scalars(q)).all())

    total = len(entries)
    correct = 0
    false_go = 0    # агент сказал go — правильно no_go (опасно)
    false_no_go = 0 # агент сказал no_go — правильно go (упущенная выгода)
    errors = 0
    details = []

    for entry in entries:
        try:
            # Берём user для компании
            user = await db.scalar(
                select(User).where(User.company_id == entry.company_id).limit(1)
            ) if entry.company_id else None

            if not user or not entry.company_id:
                errors += 1
                continue

            decision, _ = await recompute_decision_engine_v1(
                db,
                company_id=entry.company_id,
                tender_id=entry.tender_id,
                user_id=user.id,
                force=True,
            )
            agent_rec = decision.recommendation or "unknown"
            expected = entry.expected_decision

            # Нормализация: strong_go/weak → go; no_go остаётся
            def norm(r):
                if r in ("go", "strong_go", "weak_go"): return "go"
                if r in ("no_go",): return "no_go"
                return "review"

            agent_norm = norm(agent_rec)
            expected_norm = norm(expected)

            is_correct = agent_norm == expected_norm
            if is_correct:
                correct += 1
            elif agent_norm == "go" and expected_norm == "no_go":
                false_go += 1
            elif agent_norm == "no_go" and expected_norm == "go":
                false_no_go += 1

            details.append({
                "tender_id": str(entry.tender_id),
                "expected": expected_norm,
                "agent": agent_norm,
                "correct": is_correct,
            })
        except Exception as e:
            errors += 1
            details.append({"tender_id": str(entry.tender_id), "error": str(e)})

    accuracy = round(correct / total * 100, 1) if total > 0 else 0.0

    return {
        "total": total,
        "correct": correct,
        "false_go": false_go,
        "false_no_go": false_no_go,
        "errors": errors,
        "accuracy_pct": accuracy,
        "details": details,
    }
