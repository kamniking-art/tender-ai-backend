from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.policy_engine.loader import Policy
from app.policy_engine.validator import PolicyValidator

logger = logging.getLogger(__name__)

# ── Baseline policy definitions ───────────────────────────────────────────────
# Stored as plain dicts — validated through PolicyValidator before every insert.
# Edit here to change defaults; the UI (3C) will override per-company in prod.

BASE_POLICIES: list[dict] = [
    {
        "policy_type": "deadline_urgent",
        "condition": {
            "field": "deadline_hours_remaining",
            "operator": "lt",
            "value": 24,
        },
        "action": {
            "type": "block_recommendation",
            "payload": {
                "reason": "Дедлайн менее 24 часов — недостаточно времени для подготовки заявки",
                "category": "blocking",
            },
        },
        "priority": 100,
        "active": True,
    },
    {
        "policy_type": "missing_sro",
        "condition": {
            "field": "sro_ok",
            "operator": "is_false",
        },
        "action": {
            "type": "add_risk_flag",
            "payload": {
                "message": "СРО отсутствует. Уточните требования тендера — не все тендеры требуют СРО.",
                "category": "risk",
            },
        },
        "priority": 90,
        "active": True,
    },
    {
        "policy_type": "no_okved_match",
        "condition": {
            "field": "okved_match",
            "operator": "is_false",
        },
        "action": {
            "type": "add_risk_flag",
            "payload": {
                "message": "ОКВЭД компании не совпадает с требованиями тендера.",
                "category": "risk",
            },
        },
        "priority": 80,
        "active": True,
    },
    {
        "policy_type": "low_fit_score",
        "condition": {
            "field": "fit_score",
            "operator": "lt",
            "value": 30,
        },
        "action": {
            "type": "add_risk_flag",
            "payload": {
                "message": "Низкое соответствие компании тендеру (fit_score < 30).",
                "category": "risk",
            },
        },
        "priority": 70,
        "active": True,
    },
    {
        "policy_type": "high_nmck",
        "condition": {
            "field": "nmck",
            "operator": "gt",
            "value": 10_000_000,
        },
        "action": {
            "type": "require_approval",
            "payload": {
                "message": "НМЦК превышает 10 млн руб. — требуется согласование руководителя.",
                "category": "approval",
            },
        },
        "priority": 60,
        "active": True,
    },
]


async def run_seed(db: AsyncSession, company_id: UUID) -> dict:
    """Insert baseline policies for company_id. Idempotent: skips existing policy_types.

    Returns a summary dict with keys: inserted, skipped_existing, skipped_invalid.
    Never raises — invalid or duplicate policies are logged and skipped.
    """
    validator = PolicyValidator()
    inserted: list[str] = []
    skipped_existing: list[str] = []
    skipped_invalid: list[str] = []

    now = datetime.now(timezone.utc)

    for raw in BASE_POLICIES:
        policy_type = raw.get("policy_type", "unknown")

        # Attach required identity fields so validator can parse fully
        raw_full = {
            **raw,
            "policy_id": str(uuid.uuid4()),
            "company_id": str(company_id),
        }

        schema = validator.validate(raw_full)
        if schema is None:
            logger.error("Policy '%s' failed validation — skipping insert", policy_type)
            skipped_invalid.append(policy_type)
            continue

        # Idempotency check: (company_id, policy_type) must be unique
        existing = await db.scalar(
            select(Policy).where(
                Policy.company_id == company_id,
                Policy.policy_type == schema.policy_type,
            )
        )
        if existing is not None:
            logger.info("Policy '%s' already exists for company %s — skipping", policy_type, company_id)
            skipped_existing.append(policy_type)
            continue

        db.add(Policy(
            policy_id=schema.policy_id,
            company_id=schema.company_id,
            policy_type=schema.policy_type,
            condition=raw["condition"],
            action=raw["action"],
            priority=schema.priority,
            active=schema.active,
            created_at=now,
            updated_at=now,
        ))
        inserted.append(policy_type)
        logger.info("Inserting policy '%s' for company %s", policy_type, company_id)

    if inserted:
        await db.commit()

    return {
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_invalid": skipped_invalid,
    }
