from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import TestCase

from pydantic import ValidationError

from app.ai_extraction.schemas import ExtractedTenderV1
from app.risk.service import compute_risk_flags, compute_risk_score_v1


class TenderStub:
    def __init__(self) -> None:
        self.nmck = Decimal("1000000")
        self.submission_deadline = datetime.now(UTC) + timedelta(days=1)


class RiskFlagsTests(TestCase):
    def test_compute_risk_flags_detects_core_rules(self) -> None:
        extracted = ExtractedTenderV1(
            subject="Test tender",
            nmck=Decimal("1000000"),
            currency="RUB",
            submission_deadline_at=datetime.now(UTC) + timedelta(days=2),
            bid_security_required=True,
            bid_security_pct=Decimal("5.5"),
            contract_security_required=True,
            contract_security_amount=Decimal("80000"),
            qualification_requirements=["Опыт выполнения аналогичных контрактов", "СРО", "Требование 3"],
            penalties=["Штраф 0,1% за день просрочки"],
            confidence={"overall": 0.8},
            evidence={},
        )

        flags = compute_risk_flags(extracted, TenderStub())
        codes = {item["code"] for item in flags}

        self.assertIn("short_deadline", codes)
        self.assertIn("high_bid_security", codes)
        self.assertIn("high_contract_security", codes)
        self.assertIn("harsh_penalties", codes)
        self.assertIn("excessive_requirements", codes)


class SchemaValidationTests(TestCase):
    def test_schema_validation_fails_on_invalid_payload(self) -> None:
        with self.assertRaises(ValidationError):
            ExtractedTenderV1.model_validate({"schema_version": "v2", "confidence": "bad"})


class RiskScoreTests(TestCase):
    def test_risk_score_components_expected_values(self) -> None:
        extracted = ExtractedTenderV1(
            nmck=None,
            submission_deadline_at=datetime.now(UTC) + timedelta(days=2),
            bid_security_pct=Decimal("6"),
            contract_security_pct=Decimal("10"),
            penalties=["Штраф 0,1% за каждый день"],
            qualification_requirements=["СРО", "опыт выполнения", "лицензия", "требование 4"],
        )
        tender = TenderStub()
        tender.nmck = None

        risk_v1 = compute_risk_score_v1(extracted, tender)

        self.assertEqual(risk_v1["score_components"]["deadline"], 20)
        self.assertGreaterEqual(risk_v1["score_components"]["securities"], 20)
        self.assertGreaterEqual(risk_v1["score_components"]["unknowns"], 3)
        self.assertGreaterEqual(risk_v1["score_auto"], 0)
        self.assertLessEqual(risk_v1["score_auto"], 100)
