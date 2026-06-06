"""Unit tests for Tender Opportunity Report generator.

Pure function tests — no DB, no IO.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.ai_extraction.schemas import ExtractedTenderV1
from app.fit_score.schema import FitScoreComponents
from app.opportunity_report.generator import generate


def _components(**kwargs) -> FitScoreComponents:
    defaults = dict(okved=None, sro=None, license=None, experience=None, finance=None)
    defaults.update(kwargs)
    return FitScoreComponents(**defaults)


def _extracted(**kwargs) -> ExtractedTenderV1:
    return ExtractedTenderV1(schema_version="v1", **kwargs)


def _report(components=None, risk_score=None, risk_flags=None,
            extracted=None, recommendation="go", score=75):
    return generate(
        components=components or _components(),
        risk_score=risk_score,
        risk_flags=risk_flags or [],
        extracted=extracted,
        recommendation=recommendation,
        score=score,
    )


# ── strengths ─────────────────────────────────────────────────────────────────

class TestStrengths:
    def test_okved_true_in_strengths(self):
        r = _report(components=_components(okved=True))
        assert any("ОКВЭД" in s for s in r.strengths)

    def test_sro_true_in_strengths(self):
        r = _report(components=_components(sro=True))
        assert any("СРО" in s for s in r.strengths)

    def test_low_risk_score_in_strengths(self):
        r = _report(risk_score=20)
        assert any("Низкий риск" in s for s in r.strengths)

    def test_region_ok_true_in_strengths(self):
        r = _report(components=_components(region_ok=True))
        assert any("регион" in s.lower() for s in r.strengths)

    def test_far_deadline_in_strengths(self):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        r = _report(extracted=_extracted(submission_deadline_at=future))
        assert any("достаточно" in s.lower() for s in r.strengths)


# ── risks ─────────────────────────────────────────────────────────────────────

class TestRisks:
    def test_okved_false_in_risks(self):
        r = _report(components=_components(okved=False))
        assert any("ОКВЭД" in s for s in r.risks)

    def test_sro_false_in_risks(self):
        r = _report(components=_components(sro=False))
        assert any("СРО" in s for s in r.risks)

    def test_high_risk_score_in_risks(self):
        r = _report(risk_score=80)
        assert any("Высокий риск" in s for s in r.risks)

    def test_moderate_risk_score_in_risks(self):
        r = _report(risk_score=50)
        assert any("Умеренный риск" in s for s in r.risks)

    def test_risk_flag_short_deadline_in_risks(self):
        r = _report(risk_flags=[{"code": "short_deadline"}])
        assert any("дедлайн" in s.lower() for s in r.risks)

    def test_imminent_deadline_in_risks(self):
        soon = datetime.now(timezone.utc) + timedelta(hours=12)
        r = _report(extracted=_extracted(submission_deadline_at=soon))
        assert any("дн." in s for s in r.risks)

    def test_capacity_false_in_risks(self):
        r = _report(components=_components(capacity_ok=False))
        assert any("мощность" in s.lower() or "лимит" in s.lower() for s in r.risks)

    def test_economics_false_in_risks(self):
        r = _report(components=_components(economics_ok=False))
        assert any("маржа" in s.lower() or "margin" in s.lower() or "ниже" in s.lower() for s in r.risks)

    def test_risk_ok_false_in_risks(self):
        r = _report(components=_components(risk_ok=False))
        assert any("риск" in s.lower() for s in r.risks)


# ── missing_information ───────────────────────────────────────────────────────

class TestMissing:
    def test_okved_none_in_missing(self):
        r = _report(components=_components(okved=None))
        assert any("ОКВЭД" in s for s in r.missing_information)

    def test_no_risk_score_in_missing(self):
        r = _report(risk_score=None)
        assert any("риск" in s.lower() for s in r.missing_information)

    def test_no_extracted_in_missing(self):
        r = _report(extracted=None)
        assert any("документ" in s.lower() for s in r.missing_information)

    def test_sro_unknown_from_extracted_in_missing(self):
        r = _report(extracted=_extracted(sro_required=None))
        assert any("СРО" in s for s in r.missing_information)


# ── required_documents ────────────────────────────────────────────────────────

class TestRequiredDocuments:
    def test_sro_required_in_docs(self):
        r = _report(extracted=_extracted(sro_required=True))
        assert any("СРО" in d for d in r.required_documents)

    def test_qualification_requirements_in_docs(self):
        r = _report(extracted=_extracted(qualification_requirements=["Опыт 3 года"]))
        assert any("Опыт 3 года" in d for d in r.required_documents)

    def test_bid_security_in_docs(self):
        from decimal import Decimal
        r = _report(extracted=_extracted(
            bid_security_required=True,
            bid_security_pct=Decimal("5"),
        ))
        assert any("Обеспечение" in d for d in r.required_documents)

    def test_bank_guarantee_in_docs(self):
        r = _report(extracted=_extracted(bank_guarantee_required=True))
        assert any("гарантия" in d.lower() for d in r.required_documents)

    def test_no_docs_when_not_required(self):
        r = _report(extracted=_extracted(sro_required=False))
        assert not any("СРО" in d for d in r.required_documents)


# ── recommended_actions ───────────────────────────────────────────────────────

class TestActions:
    def test_go_recommendation_has_prepare_action(self):
        r = _report(recommendation="go")
        assert any("заявк" in a.lower() for a in r.recommended_actions)

    def test_strong_go_has_actions(self):
        r = _report(recommendation="strong_go")
        assert len(r.recommended_actions) >= 1

    def test_no_go_has_reason(self):
        r = _report(
            components=_components(okved=False),
            recommendation="no_go",
        )
        assert any("причина" in a.lower() or "отказ" in a.lower() or "ОКВЭД" in a for a in r.recommended_actions)

    def test_review_recommendation_has_study_action(self):
        r = _report(recommendation="review")
        assert any("изуч" in a.lower() or "документ" in a.lower() for a in r.recommended_actions)


# ── deduplication ─────────────────────────────────────────────────────────────

class TestDeduplication:
    def test_no_duplicate_strengths(self):
        r = _report(components=_components(okved=True, sro=True, license=True))
        assert len(r.strengths) == len(set(r.strengths))

    def test_no_duplicate_risks(self):
        r = _report(
            components=_components(okved=False),
            risk_flags=[{"code": "no_okved_match"}],
        )
        assert len(r.risks) == len(set(r.risks))


# ── score and recommendation passthrough ──────────────────────────────────────

class TestMetadata:
    def test_recommendation_copied(self):
        r = _report(recommendation="strong_go")
        assert r.recommendation == "strong_go"

    def test_score_copied(self):
        r = _report(score=88)
        assert r.score == 88

    def test_empty_components_no_crash(self):
        r = _report(components=_components())
        assert isinstance(r.strengths, list)
        assert isinstance(r.risks, list)
