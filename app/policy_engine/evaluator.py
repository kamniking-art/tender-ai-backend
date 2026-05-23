from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.policy_engine.schema import (
    ActionType,
    Operator,
    PolicyExecutionTrace,
    PolicySchema,
    SkipReason,
    action_severity,
)

logger = logging.getLogger(__name__)

_NULL_OPERATORS = {Operator.IS_NULL, Operator.NOT_NULL}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _evaluate_condition(operator: Operator, fact: Any, threshold: Any) -> bool:
    if operator == Operator.IS_NULL:
        return fact is None
    if operator == Operator.NOT_NULL:
        return fact is not None
    if operator == Operator.IS_TRUE:
        return fact is True
    if operator == Operator.IS_FALSE:
        return fact is False
    try:
        if operator == Operator.LT:   return fact < threshold
        if operator == Operator.LTE:  return fact <= threshold
        if operator == Operator.GT:   return fact > threshold
        if operator == Operator.GTE:  return fact >= threshold
        if operator == Operator.EQ:   return fact == threshold
        if operator == Operator.NEQ:  return fact != threshold
    except TypeError:
        logger.warning("Type error comparing fact=%r with threshold=%r using %s", fact, threshold, operator)
        return False
    return False


def _explanation_passed(policy: PolicySchema, actual: Any) -> str:
    field = policy.condition.field.value
    op = policy.condition.operator.value
    threshold = policy.condition.value
    payload = policy.action.payload
    if policy.action.type == ActionType.BLOCK_RECOMMENDATION and "reason" in payload:
        return payload["reason"]
    if policy.action.type == ActionType.ADD_RISK_FLAG and "message" in payload:
        return payload["message"]
    if policy.action.type == ActionType.ADJUST_SCORE and "delta" in payload:
        return f"Скоринг изменён на {payload['delta']:+g}: {field}={actual!r}"
    return f"{field} {op} {threshold!r} (факт: {actual!r}) → {policy.action.type.value}"


class PolicyEvaluator:
    """Pure deterministic evaluator. No IO, no DB, no LLM.

    Returns one PolicyExecutionTrace per policy — including skipped ones.
    Ordering: priority DESC, then created_at ASC for stability.
    """

    def evaluate(
        self,
        facts: dict[str, Any],
        policies: list[PolicySchema],
    ) -> list[PolicyExecutionTrace]:
        sorted_policies = sorted(
            policies,
            key=lambda p: (-p.priority, p.created_at or datetime.min),
        )

        traces: list[PolicyExecutionTrace] = []

        for policy in sorted_policies:
            field_name = policy.condition.field.value
            operator = policy.condition.operator
            fact_value = facts.get(field_name)

            # fact unavailable — explicit skipped trace
            if fact_value is None and operator not in _NULL_OPERATORS:
                logger.info("Fact '%s' not available for policy %s — skipping", field_name, policy.policy_id)
                traces.append(PolicyExecutionTrace(
                    policy_id=policy.policy_id,
                    policy_type=policy.policy_type,
                    passed=None,
                    skipped=True,
                    skip_reason=SkipReason.FACT_MISSING,
                    action_type=None,
                    severity=None,
                    score_delta=None,
                    evidence={},
                    explanation=f"Факт {field_name} недоступен",
                    evaluated_at=_now_utc(),
                ))
                continue

            passed = _evaluate_condition(operator, fact_value, policy.condition.value)

            score_delta: float | None = None
            if passed and policy.action.type == ActionType.ADJUST_SCORE:
                raw = policy.action.payload.get("delta")
                if raw is not None:
                    score_delta = float(raw)

            explanation = (
                _explanation_passed(policy, fact_value)
                if passed
                else f"Условие не выполнено: {field_name} {operator.value} {policy.condition.value!r} (факт: {fact_value!r})"
            )

            traces.append(PolicyExecutionTrace(
                policy_id=policy.policy_id,
                policy_type=policy.policy_type,
                passed=passed,
                skipped=False,
                skip_reason=SkipReason.CONDITION_NOT_MET if not passed else None,
                action_type=policy.action.type,
                severity=action_severity(policy.action.type),
                score_delta=score_delta,
                evidence={
                    "field": field_name,
                    "operator": operator.value,
                    "value": policy.condition.value,
                    "actual_value": fact_value,
                },
                explanation=explanation,
                evaluated_at=_now_utc(),
            ))

        return traces


# ── Smoke test (python -m app.policy_engine.evaluator --smoke) ────────────────

def _run_smoke() -> None:
    import json
    from uuid import uuid4
    from app.policy_engine.validator import PolicyValidator

    validator = PolicyValidator()
    raw_policies = [
        {
            "policy_id": str(uuid4()), "company_id": str(uuid4()),
            "policy_type": "deadline_check",
            "condition": {"field": "deadline_hours_remaining", "operator": "lt", "value": 24},
            "action": {"type": "block_recommendation", "payload": {"reason": "Дедлайн менее 24 часов"}},
            "priority": 100, "active": True,
        },
        {
            "policy_id": str(uuid4()), "company_id": str(uuid4()),
            "policy_type": "score_boost",
            "condition": {"field": "fit_score", "operator": "gte", "value": 80.0},
            "action": {"type": "adjust_score", "payload": {"delta": 10}},
            "priority": 50, "active": True,
        },
        {
            "policy_id": str(uuid4()), "company_id": str(uuid4()),
            "policy_type": "license_check",
            "condition": {"field": "license_ok", "operator": "is_true"},
            "action": {"type": "notify", "payload": {}},
            "priority": 10, "active": True,
        },
    ]

    policies = [p for raw in raw_policies if (p := validator.validate(raw)) is not None]

    facts: dict[str, Any] = {
        "deadline_hours_remaining": 18,
        "fit_score": 85.0,
        "sro_ok": True,
        "license_ok": None,   # fact missing — will produce fact_missing trace
        "nmck": None,
        "requirement_status": None,
        "okved_match": None,
        "funds_ok": None,
    }

    traces = PolicyEvaluator().evaluate(facts, policies)

    print("=== Policy Engine Smoke Test ===")
    print(f"Facts:    {json.dumps(facts, ensure_ascii=False)}")
    print(f"Policies: {len(policies)} loaded\n")
    for t in traces:
        if t.skipped:
            print(f"[⏭  SKIP ] {t.policy_type} | skip_reason={t.skip_reason.value} | {t.explanation}")
        elif t.passed:
            print(f"[✅ PASSED] {t.policy_type} | {t.action_type.value} | {t.explanation}")
            if t.score_delta is not None:
                print(f"           score_delta={t.score_delta:+g}")
        else:
            print(f"[❌ FAILED] {t.policy_type} | {t.skip_reason.value} | {t.explanation}")
        print(f"           evaluated_at={t.evaluated_at.isoformat()}")
    print("\nSmoke test complete.")


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        _run_smoke()
    else:
        print("Usage: python -m app.policy_engine.evaluator --smoke")
        sys.exit(1)
