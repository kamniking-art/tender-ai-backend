from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from uuid import uuid4

from app.core.config import settings
from app.document_module.service import DocumentModuleConflictError, generate_package_for_tender


class _FakeDB:
    def __init__(self) -> None:
        self.deleted = []

    async def delete(self, obj):
        self.deleted.append(obj)

    def add(self, _obj):
        return None

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


class DocumentModuleIntegrationTests(IsolatedAsyncioTestCase):
    async def test_generate_package_force_and_conflict(self) -> None:
        fake_db = _FakeDB()
        company_id = uuid4()
        tender_id = uuid4()
        user_id = uuid4()

        tender = SimpleNamespace(id=tender_id, company_id=company_id, title="Tender A", nmck=Decimal("1200000"), submission_deadline=datetime.now(UTC))
        decision = SimpleNamespace(
            recommendation="go",
            need_bid_security=True,
            need_contract_security=False,
        )
        company = SimpleNamespace(
            id=company_id,
            ingestion_settings={
                "profile": {
                    "legal_name": "ООО Гранит",
                    "inn": "7800000000",
                    "legal_address": "СПб",
                    "director_name": "Иванов И.И.",
                    "phone": "+7",
                    "email": "test@example.com",
                }
            },
        )
        analysis = SimpleNamespace(
            requirements={
                "extracted_v1": {
                    "qualification_requirements": ["Опыт выполнения"],
                    "bid_security_required": True,
                }
            },
            risk_flags=[],
            updated_by=None,
        )

        created_docs = []
        expected_company_id = company_id
        expected_tender_id = tender_id

        from app.document_module import service as doc_service

        old_storage_root = settings.storage_root
        old_get_tender = doc_service.get_tender_by_id_scoped
        old_get_decision = doc_service.get_decision_scoped
        old_get_company = doc_service._get_company_scoped
        old_get_analysis = doc_service.get_analysis_scoped
        old_create_doc = doc_service.create_document_from_bytes
        old_list_docs = doc_service._list_package_documents
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                settings.storage_root = tmp_dir

                async def _mock_get_tender(db, company, tid):
                    self.assertIs(db, fake_db)
                    self.assertEqual(company, expected_company_id)
                    self.assertEqual(tid, expected_tender_id)
                    return tender

                async def _mock_get_decision(db, company, tid):
                    self.assertIs(db, fake_db)
                    self.assertEqual(company, expected_company_id)
                    self.assertEqual(tid, expected_tender_id)
                    return decision

                async def _mock_get_company(db, company_uuid):
                    self.assertIs(db, fake_db)
                    self.assertEqual(company_uuid, expected_company_id)
                    return company

                async def _mock_get_analysis(db, company, tid):
                    self.assertIs(db, fake_db)
                    self.assertEqual(company, expected_company_id)
                    self.assertEqual(tid, expected_tender_id)
                    return analysis

                async def _mock_list_docs(_db, _company, _tender):
                    return list(created_docs)

                async def _mock_create_document_from_bytes(
                    db,
                    *,
                    company_id,
                    tender_id,
                    uploaded_by,
                    file_name,
                    content,
                    content_type,
                    doc_type,
                    relative_path_override=None,
                ):
                    self.assertIs(db, fake_db)
                    self.assertEqual(company_id, expected_company_id)
                    self.assertEqual(tender_id, expected_tender_id)
                    self.assertEqual(uploaded_by, user_id)
                    rel_path = relative_path_override or file_name
                    abs_path = Path(settings.storage_root) / rel_path
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    abs_path.write_bytes(content)
                    doc = SimpleNamespace(
                        id=uuid4(),
                        file_name=file_name,
                        storage_path=rel_path,
                        content_type=content_type,
                        file_size=len(content),
                        uploaded_at=datetime.now(UTC),
                    )
                    created_docs.append(doc)
                    return doc

                doc_service.get_tender_by_id_scoped = _mock_get_tender
                doc_service.get_decision_scoped = _mock_get_decision
                doc_service._get_company_scoped = _mock_get_company
                doc_service.get_analysis_scoped = _mock_get_analysis
                doc_service.create_document_from_bytes = _mock_create_document_from_bytes
                doc_service._list_package_documents = _mock_list_docs

                generated_files, checklist = await generate_package_for_tender(
                    fake_db,
                    company_id=company_id,
                    tender_id=tender_id,
                    user_id=user_id,
                    force=False,
                )
                self.assertEqual(len(generated_files), 3)
                self.assertTrue(checklist)
                for item in created_docs:
                    self.assertTrue((Path(settings.storage_root) / item.storage_path).exists())

                with self.assertRaises(DocumentModuleConflictError):
                    await generate_package_for_tender(
                        fake_db,
                        company_id=company_id,
                        tender_id=tender_id,
                        user_id=user_id,
                        force=False,
                    )

                await generate_package_for_tender(
                    fake_db,
                    company_id=company_id,
                    tender_id=tender_id,
                    user_id=user_id,
                    force=True,
                )
                self.assertTrue(created_docs)
        finally:
            settings.storage_root = old_storage_root
            doc_service.get_tender_by_id_scoped = old_get_tender
            doc_service.get_decision_scoped = old_get_decision
            doc_service._get_company_scoped = old_get_company
            doc_service.get_analysis_scoped = old_get_analysis
            doc_service.create_document_from_bytes = old_create_doc
            doc_service._list_package_documents = old_list_docs
