from __future__ import annotations

from datetime import datetime
from unittest import TestCase
from uuid import uuid4

from app.policy_engine.evaluator import PolicyEvaluator
from app.policy_engine.schema import (
    ActionType,
    PolicyExecutionTrace,
    PolicySchema,
    SkipReason,
)
from app.policy_engine.validator import PolicyValidator


def _make_policy(
    *,
    field: str = "deadline_hours_remaining",
    operator: str = "lt",
    value=24,
    action_type: str = "block_recommendation",
    payload: dict | None = None,
    priority: int = 0,
    active: bool = True,
) -> dict:
    return {
        "policy_id": str(uuid4()),
        "company_id": str(uuid4()),
        "policy_type": "test_policy",
        "condition": {"field": field, "operator": operator, "value": value},
        "action": {"type": action_type, "payload": payload or {}},
        "priority": priority,
        "active": active,
    }


def _validated(*raw_policies: dict) -> list[PolicySchema]:
    v = PolicyValidator()
    return [p for raw in raw_policies if (p := v.validate(raw)) is not None]


class TestPolicyEngine(TestCase):

    def setUp(self) -> None:
        self.evaluator = PolicyEvaluator()
        self.validator = PolicyValidator()

    # 1 ── condition passes ──────────────────────────────────────────────────
    def test_valid_condition_passes(self) -> None:
        policies = _validated(_make_policy(
            operator="lt", value=24,
            action_type="block_recommendation",
            payload={"reason": "Дедлайн менее 24 часов"},
        ))
        results = self.evaluator.evaluate({"deadline_hours_remaining": 18}, policies)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r.passed)
        self.assertFalse(r.skipped)
        self.assertIsNone(r.skip_reason)
        self.assertEqual(r.action_type, ActionType.BLOCK_RECOMMENDATION)

    # 2 ── condition does not pass ───────────────────────────────────────────
    def test_valid_condition_fails(self) -> None:
        policies = _validated(_make_policy(operator="lt", value=24))
        results = self.evaluator.evaluate({"deadline_hours_remaining": 48}, policies)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertFalse(r.passed)
        self.assertFalse(r.skipped)
        self.assertEqual(r.skip_reason, SkipReason.CONDITION_NOT_MET)

    # 3 ── invalid policy skipped by validator ───────────────────────────────
    def test_invalid_policy_skipped(self) -> None:
        bad = {
            "policy_id": str(uuid4()), "company_id": str(uuid4()),
            "policy_type": "bad",
            "condition": {"field": "some_unknown_field", "operator": "maybe"},
            "action": {"type": "block_recommendation"},
        }
        self.assertIsNone(self.validator.validate(bad))
        self.assertEqual(self.evaluator.evaluate({"deadline_hours_remaining": 18}, []), [])

    # 4 ── None fact → explicit skipped trace ────────────────────────────────
    def test_none_fact_skipped(self) -> None:
        policies = _validated(_make_policy(
            field="fit_score", operator="lt", value=50,
            action_type="block_recommendation",
        ))
        results = self.evaluator.evaluate({"fit_score": None}, policies)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r.skipped)
        self.assertIsNone(r.passed)
        self.assertEqual(r.skip_reason, SkipReason.FACT_MISSING)

    # 5 ── priority order is respected ───────────────────────────────────────
    def test_priority_order(self) -> None:
        policies = _validated(
            _make_policy(priority=10,  field="fit_score", operator="gte", value=0, action_type="notify"),
            _make_policy(priority=100, field="fit_score", operator="gte", value=0, action_type="block_recommendation"),
            _make_policy(priority=50,  field="fit_score", operator="gte", value=0, action_type="add_risk_flag"),
        )
        results = self.evaluator.evaluate({"fit_score": 60.0}, policies)

        self.assertEqual(len(results), 3)
        self.assertEqual([r.action_type for r in results], [
            ActionType.BLOCK_RECOMMENDATION,
            ActionType.ADD_RISK_FLAG,
            ActionType.NOTIFY,
        ])

    # 6 ── score_delta aggregated ────────────────────────────────────────────
    def test_score_delta_aggregated(self) -> None:
        policies = _validated(
            _make_policy(field="fit_score", operator="gte", value=0,
                         action_type="adjust_score", payload={"delta": -10}),
            _make_policy(field="fit_score", operator="gte", value=0,
                         action_type="adjust_score", payload={"delta": -5}),
        )
        results = self.evaluator.evaluate({"fit_score": 70.0}, policies)

        deltas = [r.score_delta for r in results if r.score_delta is not None]
        self.assertEqual(sum(deltas), -15.0)

    # 7 ── block does not stop evaluation ────────────────────────────────────
    def test_block_does_not_stop_evaluation(self) -> None:
        policies = _validated(
            _make_policy(operator="lt", value=24, action_type="block_recommendation", priority=100),
            _make_policy(field="fit_score", operator="lt", value=50, action_type="add_risk_flag", priority=50),
        )
        results = self.evaluator.evaluate(
            {"deadline_hours_remaining": 12, "fit_score": 30.0}, policies
        )

        self.assertEqual(len(results), 2)
        types = {r.action_type for r in results}
        self.assertIn(ActionType.BLOCK_RECOMMENDATION, types)
        self.assertIn(ActionType.ADD_RISK_FLAG, types)

    # 8 ── inactive policy excluded by loader before reaching evaluator ──────
    def test_inactive_policy_skipped(self) -> None:
        schema = self.validator.validate(_make_policy(active=False))
        self.assertIsNotNone(schema)

        active_policies = [p for p in [schema] if p is not None and p.active]
        self.assertEqual(active_policies, [])

        results = self.evaluator.evaluate({"deadline_hours_remaining": 10}, active_policies)
        self.assertEqual(results, [])

    # 9 ── fact_missing produces correct skip_reason ─────────────────────────
    def test_fact_missing_skip_reason(self) -> None:
        policies = _validated(_make_policy(
            field="nmck", operator="gt", value=1_000_000,
            action_type="add_risk_flag",
        ))
        # nmck is missing from facts entirely
        results = self.evaluator.evaluate({}, policies)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r.skipped)
        self.assertIsNone(r.passed)
        self.assertEqual(r.skip_reason, SkipReason.FACT_MISSING)
        self.assertIsNone(r.action_type)
        self.assertEqual(r.evidence, {})

    # 10 ── condition_not_met has correct skip_reason ────────────────────────
    def test_condition_not_met_skip_reason(self) -> None:
        policies = _validated(_make_policy(
            field="fit_score", operator="gte", value=80,
            action_type="block_recommendation",
        ))
        results = self.evaluator.evaluate({"fit_score": 50.0}, policies)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertFalse(r.passed)
        self.assertFalse(r.skipped)
        self.assertEqual(r.skip_reason, SkipReason.CONDITION_NOT_MET)

    # 11 ── evaluated_at is a datetime in every trace ────────────────────────
    def test_evaluated_at_present(self) -> None:
        policies = _validated(
            _make_policy(operator="lt", value=24, action_type="block_recommendation"),
            _make_policy(field="fit_score", operator="gt", value=0, action_type="notify"),
        )
        facts = {
            "deadline_hours_remaining": 18,
            "fit_score": None,   # will produce a fact_missing trace
        }
        results = self.evaluator.evaluate(facts, policies)

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertIsInstance(r.evaluated_at, datetime)
