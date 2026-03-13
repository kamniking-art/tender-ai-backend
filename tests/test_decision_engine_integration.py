from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from uuid import uuid4

from app.decision_engine.service import recompute_decision_engine_v1


class _FakeDB:
    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


class DecisionEngineIntegrationTests(IsolatedAsyncioTestCase):
    async def test_recompute_uses_analysis_auto_risk_and_sets_engine_meta(self) -> None:
        fake_db = _FakeDB()
        company_id = uuid4()
        tender_id = uuid4()
        user_id = uuid4()

        tender = SimpleNamespace(
            id=tender_id,
            company_id=company_id,
            nmck=Decimal("1000000"),
            title="Поставка гранитных плит для мемориала",
            customer_name="Администрация",
            external_id="test-1",
        )
        decision = SimpleNamespace(
            recommendation="unsure",
            expected_margin_pct=Decimal("25"),
            expected_margin_value=Decimal("100000"),
            risk_score=None,
            bid_security_amount=None,
            contract_security_amount=None,
            nmck=None,
            decision_score=None,
            recommendation_reason=None,
            priority_score=None,
            priority_label=None,
            priority_reason=None,
            engine_meta={},
            updated_by=None,
        )
        analysis = SimpleNamespace(
            requirements={
                "risk_v1": {"score_auto": 30},
                "extracted_v1": {
                    "schema_version": "v1",
                    "subject": "Поставка гранитных плит",
                    "nmck": "1000000",
                    "currency": "RUB",
                    "submission_deadline_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
                    "bid_security_required": False,
                    "bid_security_amount": None,
                    "bid_security_pct": None,
                    "contract_security_required": False,
                    "contract_security_amount": None,
                    "contract_security_pct": None,
                    "qualification_requirements": [],
                    "tech_parameters": ["гранитная плита"],
                    "penalties": [],
                    "confidence": {"overall": 0.5},
                    "evidence": {},
                },
            },
            risk_flags=[],
        )
        finance = SimpleNamespace(
            cost_estimate=Decimal("700000"),
            participation_cost=Decimal("5000"),
            win_probability=Decimal("40"),
        )

        from app.decision_engine import service as engine_service

        old_get_tender = engine_service.get_tender_by_id_scoped
        old_get_or_create = engine_service._get_or_create_decision
        old_get_analysis = engine_service._get_analysis_scoped
        old_get_finance = engine_service._get_finance_scoped
        old_docs_count = engine_service._documents_count
        try:
            async def _mock_get_tender(db, company, tid):
                self.assertIs(db, fake_db)
                self.assertEqual(company, company_id)
                self.assertEqual(tid, tender_id)
                return tender

            async def _mock_get_or_create(db, company, tid, uid):
                self.assertIs(db, fake_db)
                self.assertEqual(company, company_id)
                self.assertEqual(tid, tender_id)
                self.assertEqual(uid, user_id)
                return decision

            async def _mock_get_analysis(db, company, tid):
                self.assertIs(db, fake_db)
                self.assertEqual(company, company_id)
                self.assertEqual(tid, tender_id)
                return analysis

            async def _mock_get_finance(db, company, tid):
                self.assertIs(db, fake_db)
                self.assertEqual(company, company_id)
                self.assertEqual(tid, tender_id)
                return finance

            async def _mock_docs_count(db, company, tid):
                self.assertIs(db, fake_db)
                self.assertEqual(company, company_id)
                self.assertEqual(tid, tender_id)
                return 2

            engine_service.get_tender_by_id_scoped = _mock_get_tender
            engine_service._get_or_create_decision = _mock_get_or_create
            engine_service._get_analysis_scoped = _mock_get_analysis
            engine_service._get_finance_scoped = _mock_get_finance
            engine_service._documents_count = _mock_docs_count

            updated_decision, engine_meta = await recompute_decision_engine_v1(
                fake_db,
                company_id=company_id,
                tender_id=tender_id,
                user_id=user_id,
                force=False,
            )
        finally:
            engine_service.get_tender_by_id_scoped = old_get_tender
            engine_service._get_or_create_decision = old_get_or_create
            engine_service._get_analysis_scoped = old_get_analysis
            engine_service._get_finance_scoped = old_get_finance
            engine_service._documents_count = old_docs_count

        self.assertIn(updated_decision.recommendation, {"go", "strong_go"})
        self.assertIn(engine_meta["recommendation"], {"go", "strong_go"})
        self.assertIn("finance", engine_meta)
        self.assertIn("decision_score", engine_meta)
        self.assertIn("priority", engine_meta)
        self.assertIsNotNone(updated_decision.priority_label)
        self.assertEqual(updated_decision.engine_meta["score"], engine_meta["score"])
