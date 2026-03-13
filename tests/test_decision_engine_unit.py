from __future__ import annotations

from decimal import Decimal
from unittest import TestCase

from app.decision_engine.service import (
    compute_decision_engine_v1,
    compute_finance_v2,
)


class DecisionEngineUnitTests(TestCase):
    def test_high_relevance_low_risk_is_go(self) -> None:
        result = compute_decision_engine_v1(
            relevance_score=85,
            matched_keywords=["гранит", "мемориал"],
            nmck=Decimal("2400000"),
            has_documents=True,
            risk_score=20,
            category="камень / гранит / памятники",
        )
        self.assertEqual(result["recommendation"], "go")
        self.assertGreaterEqual(result["score"], 70)

    def test_low_relevance_high_risk_is_no_go(self) -> None:
        result = compute_decision_engine_v1(
            relevance_score=20,
            matched_keywords=["поставка"],
            nmck=Decimal("150000"),
            has_documents=False,
            risk_score=85,
            category="нерелевантно / прочее",
        )
        self.assertEqual(result["recommendation"], "no_go")
        self.assertLess(result["score"], 30)

    def test_mid_case_is_review_or_weak(self) -> None:
        result = compute_decision_engine_v1(
            relevance_score=55,
            matched_keywords=["плитка"],
            nmck=Decimal("600000"),
            has_documents=False,
            risk_score=45,
            category="строительные материалы",
        )
        self.assertIn(result["recommendation"], {"review", "weak"})
        self.assertGreaterEqual(result["score"], 30)

    def test_flags_reduce_score(self) -> None:
        base = compute_decision_engine_v1(
            relevance_score=80,
            matched_keywords=["гранит", "памятник"],
            nmck=Decimal("3000000"),
            has_documents=True,
            risk_score=30,
        )
        penalized = compute_decision_engine_v1(
            relevance_score=80,
            matched_keywords=["гранит", "памятник"],
            nmck=Decimal("3000000"),
            has_documents=True,
            risk_score=70,
        )
        self.assertLess(penalized["score"], base["score"])

    def test_finance_requires_analysis_when_price_missing(self) -> None:
        finance = compute_finance_v2(
            contract_price=None,
            cost_estimate=Decimal("100"),
            participation_cost=Decimal("10"),
            win_probability_pct=Decimal("40"),
        )
        self.assertEqual(finance["finance_recommendation"], "requires_analysis")

    def test_finance_no_go_when_negative_margin(self) -> None:
        finance = compute_finance_v2(
            contract_price=Decimal("100"),
            cost_estimate=Decimal("120"),
            participation_cost=Decimal("0"),
            win_probability_pct=Decimal("50"),
        )
        self.assertEqual(finance["finance_recommendation"], "no_go")

    def test_finance_no_go_when_negative_ev(self) -> None:
        finance = compute_finance_v2(
            contract_price=Decimal("1000"),
            cost_estimate=Decimal("900"),
            participation_cost=Decimal("70"),
            win_probability_pct=Decimal("40"),
        )
        self.assertEqual(finance["finance_recommendation"], "no_go")

    def test_finance_go_when_ev_and_margin_good(self) -> None:
        finance = compute_finance_v2(
            contract_price=Decimal("1000"),
            cost_estimate=Decimal("700"),
            participation_cost=Decimal("50"),
            win_probability_pct=Decimal("40"),
        )
        self.assertEqual(finance["finance_recommendation"], "go")

    def test_finance_requires_analysis_when_incomplete(self) -> None:
        finance = compute_finance_v2(
            contract_price=Decimal("1000"),
            cost_estimate=None,
            participation_cost=Decimal("10"),
            win_probability_pct=Decimal("50"),
        )
        self.assertEqual(finance["finance_recommendation"], "requires_analysis")
