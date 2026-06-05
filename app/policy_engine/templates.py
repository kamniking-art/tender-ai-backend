"""
Библиотека отраслевых шаблонов политик.
Каждый шаблон — набор политик для конкретной отрасли.
Добавить новую отрасль = добавить новый блок в POLICY_TEMPLATES.
"""
from uuid import uuid4
from datetime import datetime, timezone

POLICY_TEMPLATES: dict[str, dict] = {

    "electrotechnical": {
        "name": "Поставка электротехнических товаров",
        "description": "Поставка кабельной продукции, оборудования, комплектующих. СРО не требуется.",
        "policies": [
            {
                "policy_type": "deadline_urgent",
                "condition": {"field": "deadline_hours_remaining", "operator": "lt", "value": 48},
                "action": {"type": "add_risk_flag", "payload": {"message": "До дедлайна менее 48ч — недостаточно времени для подготовки поставки.", "category": "risk"}},
                "priority": 100, "active": True,
            },
            {
                "policy_type": "low_fit_score",
                "condition": {"field": "fit_score", "operator": "lt", "value": 40},
                "action": {"type": "add_risk_flag", "payload": {"message": "Низкое соответствие профилю компании требованиям тендера.", "category": "risk"}},
                "priority": 80, "active": True,
            },
            {
                "policy_type": "high_nmck",
                "condition": {"field": "nmck", "operator": "gt", "value": 5000000},
                "action": {"type": "require_approval", "payload": {"message": "НМЦК превышает 5 млн руб. — требуется согласование.", "category": "approval"}},
                "priority": 70, "active": True,
            },
            {
                "policy_type": "no_okved_match",
                "condition": {"field": "okved_match", "operator": "is_false"},
                "action": {"type": "add_risk_flag", "payload": {"message": "ОКВЭД компании не совпадает с тендером — уточните соответствие.", "category": "risk"}},
                "priority": 60, "active": True,
            },
        ],
    },

    "granite": {
        "name": "Гранит, памятники и изделия из камня",
        "description": "Поставка и установка гранитных изделий, надгробий, благоустройство. СРО не требуется.",
        "policies": [
            {
                "policy_type": "deadline_urgent",
                "condition": {"field": "deadline_hours_remaining", "operator": "lt", "value": 72},
                "action": {"type": "add_risk_flag", "payload": {"message": "До дедлайна менее 72ч — мало времени для согласования производства.", "category": "risk"}},
                "priority": 100, "active": True,
            },
            {
                "policy_type": "low_fit_score",
                "condition": {"field": "fit_score", "operator": "lt", "value": 35},
                "action": {"type": "add_risk_flag", "payload": {"message": "Низкое соответствие — проверьте технические требования к изделиям.", "category": "risk"}},
                "priority": 80, "active": True,
            },
            {
                "policy_type": "high_nmck",
                "condition": {"field": "nmck", "operator": "gt", "value": 3000000},
                "action": {"type": "require_approval", "payload": {"message": "НМЦК превышает 3 млн руб. — требуется согласование руководителя.", "category": "approval"}},
                "priority": 70, "active": True,
            },
            {
                "policy_type": "no_okved_match",
                "condition": {"field": "okved_match", "operator": "is_false"},
                "action": {"type": "add_risk_flag", "payload": {"message": "ОКВЭД не совпадает — убедитесь что тендер по профилю компании.", "category": "risk"}},
                "priority": 60, "active": True,
            },
        ],
    },

    "construction": {
        "name": "Строительство и ремонт",
        "description": "Строительные работы, капремонт, реконструкция. СРО обязательно.",
        "policies": [
            {
                "policy_type": "missing_sro",
                "condition": {"field": "sro_ok", "operator": "is_false"},
                "action": {"type": "block_recommendation", "payload": {"reason": "СРО обязательно для строительных работ.", "category": "blocking"}},
                "priority": 100, "active": True,
            },
            {
                "policy_type": "deadline_urgent",
                "condition": {"field": "deadline_hours_remaining", "operator": "lt", "value": 48},
                "action": {"type": "add_risk_flag", "payload": {"message": "До дедлайна менее 48ч — риск не успеть подготовить пакет документов.", "category": "risk"}},
                "priority": 90, "active": True,
            },
            {
                "policy_type": "high_nmck",
                "condition": {"field": "nmck", "operator": "gt", "value": 10000000},
                "action": {"type": "require_approval", "payload": {"message": "НМЦК превышает 10 млн руб. — требуется согласование.", "category": "approval"}},
                "priority": 80, "active": True,
            },
            {
                "policy_type": "low_fit_score",
                "condition": {"field": "fit_score", "operator": "lt", "value": 30},
                "action": {"type": "add_risk_flag", "payload": {"message": "Низкое соответствие профилю — проверьте квалификационные требования.", "category": "risk"}},
                "priority": 70, "active": True,
            },
            {
                "policy_type": "no_okved_match",
                "condition": {"field": "okved_match", "operator": "is_false"},
                "action": {"type": "add_risk_flag", "payload": {"message": "ОКВЭД не совпадает с видом работ тендера.", "category": "risk"}},
                "priority": 60, "active": True,
            },
        ],
    },

    "supply": {
        "name": "Поставка товаров (универсальный)",
        "description": "Универсальный шаблон для любых товарных поставок. Без СРО.",
        "policies": [
            {
                "policy_type": "deadline_urgent",
                "condition": {"field": "deadline_hours_remaining", "operator": "lt", "value": 24},
                "action": {"type": "add_risk_flag", "payload": {"message": "До дедлайна менее 24ч.", "category": "risk"}},
                "priority": 100, "active": True,
            },
            {
                "policy_type": "high_nmck",
                "condition": {"field": "nmck", "operator": "gt", "value": 10000000},
                "action": {"type": "require_approval", "payload": {"message": "НМЦК превышает 10 млн руб.", "category": "approval"}},
                "priority": 80, "active": True,
            },
            {
                "policy_type": "low_fit_score",
                "condition": {"field": "fit_score", "operator": "lt", "value": 30},
                "action": {"type": "add_risk_flag", "payload": {"message": "Низкое соответствие профилю компании.", "category": "risk"}},
                "priority": 70, "active": True,
            },
            {
                "policy_type": "no_okved_match",
                "condition": {"field": "okved_match", "operator": "is_false"},
                "action": {"type": "add_risk_flag", "payload": {"message": "ОКВЭД не совпадает с тематикой тендера.", "category": "risk"}},
                "priority": 60, "active": True,
            },
        ],
    },

    "services": {
        "name": "Услуги (универсальный)",
        "description": "Универсальный шаблон для сервисных контрактов.",
        "policies": [
            {
                "policy_type": "deadline_urgent",
                "condition": {"field": "deadline_hours_remaining", "operator": "lt", "value": 24},
                "action": {"type": "add_risk_flag", "payload": {"message": "До дедлайна менее 24ч.", "category": "risk"}},
                "priority": 100, "active": True,
            },
            {
                "policy_type": "high_nmck",
                "condition": {"field": "nmck", "operator": "gt", "value": 10000000},
                "action": {"type": "require_approval", "payload": {"message": "НМЦК превышает 10 млн руб.", "category": "approval"}},
                "priority": 80, "active": True,
            },
            {
                "policy_type": "low_fit_score",
                "condition": {"field": "fit_score", "operator": "lt", "value": 30},
                "action": {"type": "add_risk_flag", "payload": {"message": "Низкое соответствие профилю компании.", "category": "risk"}},
                "priority": 70, "active": True,
            },
        ],
    },
}


async def apply_template(db, company_id, template_key: str) -> dict:
    """Применить шаблон политик к компании. Пропускает уже существующие."""
    from app.policy_engine.seed import run_seed as _run_seed_base
    from app.policy_engine.loader import Policy
    from sqlalchemy import select
    from datetime import datetime, timezone
    import uuid as _uuid

    template = POLICY_TEMPLATES.get(template_key)
    if not template:
        return {"error": f"Template '{template_key}' not found"}

    inserted, skipped_existing = [], []
    now = datetime.now(timezone.utc)

    for raw in template["policies"]:
        policy_type = raw["policy_type"]
        existing = await db.scalar(select(Policy).where(
            Policy.company_id == company_id,
            Policy.policy_type == policy_type,
        ))
        if existing:
            skipped_existing.append(policy_type)
            continue

        db.add(Policy(
            policy_id=_uuid.uuid4(),
            company_id=company_id,
            policy_type=policy_type,
            condition=raw["condition"],
            action=raw["action"],
            priority=raw["priority"],
            active=raw["active"],
            created_at=now,
            updated_at=now,
        ))
        inserted.append(policy_type)

    if inserted:
        await db.commit()

    return {
        "template": template_key,
        "template_name": template["name"],
        "inserted": inserted,
        "skipped_existing": skipped_existing,
    }
