from __future__ import annotations

from decimal import Decimal
from unittest import TestCase

from app.decision_engine.service import compute_decision_engine_v1


class DecisionEngineUnitTests(TestCase):
    def test_margin_25_risk_30_no_flags_go(self) -> None:
        result = compute_decision_engine_v1(
            margin_pct=Decimal("25"),
            margin_value=Decimal("100000"),
            risk_score=30,
            short_deadline=False,
            harsh_penalties=False,
            high_security=False,
        )
        self.assertEqual(result["recommendation"], "go")
        self.assertEqual(result["score"], 40)

    def test_margin_5_risk_85_no_go(self) -> None:
        result = compute_decision_engine_v1(
            margin_pct=Decimal("5"),
            margin_value=Decimal("30000"),
            risk_score=85,
            short_deadline=False,
            harsh_penalties=False,
            high_security=False,
        )
        self.assertEqual(result["recommendation"], "no_go")
        self.assertLessEqual(result["score"], -10)

    def test_margin_null_risk_null_unsure(self) -> None:
        result = compute_decision_engine_v1(
            margin_pct=None,
            margin_value=None,
            risk_score=None,
            short_deadline=False,
            harsh_penalties=False,
            high_security=False,
        )
        self.assertEqual(result["recommendation"], "unsure")
        self.assertEqual(result["score"], -5)

    def test_flags_reduce_score(self) -> None:
        base = compute_decision_engine_v1(
            margin_pct=Decimal("25"),
            margin_value=Decimal("100000"),
            risk_score=30,
            short_deadline=False,
            harsh_penalties=False,
            high_security=False,
        )
        penalized = compute_decision_engine_v1(
            margin_pct=Decimal("25"),
            margin_value=Decimal("100000"),
            risk_score=30,
            short_deadline=True,
            harsh_penalties=False,
            high_security=True,
        )
        self.assertLess(penalized["score"], base["score"])
