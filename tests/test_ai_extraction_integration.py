from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from uuid import uuid4

from app.ai_extraction.service import run_extraction
from app.core.config import settings


class _FakeDB:
    def __init__(self) -> None:
        self.added = None

    async def scalar(self, _stmt):
        return None

    def add(self, obj):
        self.added = obj

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


class AIExtractionIntegrationMockTests(IsolatedAsyncioTestCase):
    async def test_run_extraction_mock_creates_ready_analysis(self) -> None:
        fake_db = _FakeDB()
        company_id = uuid4()
        user_id = uuid4()
        tender_id = uuid4()

        tender = SimpleNamespace(
            id=tender_id,
            company_id=company_id,
            nmck=Decimal("1000000"),
            submission_deadline=datetime.now(UTC) + timedelta(days=2),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_path = Path(tmp_dir) / "terms.txt"
            txt_path.write_text(
                """
                Тендер на поставку гранитных плит.
                НМЦК 1 250 000 руб.
                Срок подачи: 2026-03-05 12:00.
                Обеспечение заявки 6%.
                Штраф 0,1% за каждый день просрочки.
                Требуется подтверждение опыта выполнения аналогичных контрактов.
                Требуется наличие специалистов и материально-технической базы.
                Требуется описание технологических параметров поставляемых изделий.
                Требуется соблюдение сроков поставки и гарантийных обязательств.
                Требуется обеспечение исполнения обязательств по контракту.
                Дополнительные условия по качеству продукции и срокам приемки.
                Участник закупки должен предоставить сведения о квалификации сотрудников.
                Участник закупки должен предоставить сведения о выполненных ранее поставках.
                Участник закупки должен предоставить сведения о финансовой устойчивости.
                Участник закупки должен предоставить сведения о наличии производственных ресурсов.
                Участник закупки должен предоставить сведения о системе контроля качества.
                Участник закупки должен предоставить сведения о гарантийном обслуживании.
                """,
                encoding="utf-8",
            )

            doc = SimpleNamespace(
                id=uuid4(),
                company_id=company_id,
                tender_id=tender_id,
                file_name="terms.txt",
                storage_path="terms.txt",
            )

            from app.ai_extraction import service as extraction_service

            old_mode = settings.ai_extractor_mode
            old_storage_root = settings.storage_root
            old_get_tender = extraction_service.get_tender_by_id_scoped
            old_resolve_docs = extraction_service._resolve_documents
            try:
                settings.ai_extractor_mode = "mock"
                settings.storage_root = tmp_dir

                async def _mock_get_tender(db, company, tender_uuid):
                    self.assertIs(db, fake_db)
                    self.assertEqual(company, company_id)
                    self.assertEqual(tender_uuid, tender_id)
                    return tender

                async def _mock_resolve_documents(db, *, company_id, tender_id, document_ids):
                    self.assertIs(db, fake_db)
                    self.assertEqual(company_id, doc.company_id)
                    self.assertEqual(tender_id, doc.tender_id)
                    self.assertIsNone(document_ids)
                    return [doc]

                extraction_service.get_tender_by_id_scoped = _mock_get_tender
                extraction_service._resolve_documents = _mock_resolve_documents

                analysis, extracted = await run_extraction(
                    fake_db,
                    company_id=company_id,
                    user_id=user_id,
                    tender_id=tender_id,
                    document_ids=None,
                )
            finally:
                settings.ai_extractor_mode = old_mode
                settings.storage_root = old_storage_root
                extraction_service.get_tender_by_id_scoped = old_get_tender
                extraction_service._resolve_documents = old_resolve_docs

        self.assertEqual(analysis.status, "ready")
        self.assertIn("extracted_v1", analysis.requirements)
        self.assertIn("risk_v1", analysis.requirements)
        self.assertGreaterEqual(analysis.requirements["risk_v1"]["score_auto"], 0)
        self.assertLessEqual(analysis.requirements["risk_v1"]["score_auto"], 100)
        self.assertIsNotNone(extracted.schema_version)
        self.assertEqual(fake_db.added, analysis)
