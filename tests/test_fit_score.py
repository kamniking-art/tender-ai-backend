"""Unit tests for FitScorer.

Pure function tests — no DB, no IO required.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.ai_extraction.schemas import ExtractedTenderV1
from app.fit_score.scorer import FitScorer
from app.requirements.normalizer import RequirementNormalizer
from app.requirements.schema import NormalizedRequirement, RequirementType

scorer = FitScorer()


def _checklist(
    sro_req: bool = False,
    license_req: bool = False,
    experience_req: bool = False,
) -> list[NormalizedRequirement]:
    """Build a minimal checklist with controllable required flags."""
    normalizer = RequirementNormalizer()
    qual_parts: list[str] = []
    if sro_req:
        qual_parts.append("наличие СРО обязательно")
    if license_req:
        qual_parts.append("лицензия МЧС обязательна")
    if experience_req:
        qual_parts.append("опыт выполнения аналогичных работ")
    return normalizer.normalize(
        ExtractedTenderV1(qualification_requirements=qual_parts)
    )


# ── 1. All components True → fit_score = 100 ─────────────────────────────────

def test_all_ok():
    profile = {
        "okved_main": "41.20",
        "sro": {"has_sro": True},
        "licenses": [{"active": True, "name": "Лицензия МЧС"}],
        "experience": {"years": 5, "contracts": 10},
        "financial": {"available_funds": 1_000_000},
    }
    extracted = ExtractedTenderV1(
        subject="строительство 41.20",
        bid_security_required=True,
        bid_security_amount=Decimal("500000"),
        qualification_requirements=[
            "наличие СРО обязательно",
            "лицензия МЧС обязательна",
            "опыт выполнения аналогичных работ",
        ],
    )
    checklist = _checklist(sro_req=True, license_req=True, experience_req=True)
    result = scorer.score(profile, checklist, extracted)
    assert result.fit_score == 100.0
    assert result.components.okved is True
    assert result.components.sro is True
    assert result.components.license is True
    assert result.components.experience is True
    assert result.components.finance is True


# ── 2. All components False → fit_score = 0 ──────────────────────────────────

def test_all_false():
    profile = {
        "okved_main": "99.99",           # won't match tender subject
        "sro": {"has_sro": False},
        "licenses": [{"active": False, "name": "expired"}],
        "experience": {},                # empty dict → False when required
        "financial": {"available_funds": 100},
    }
    extracted = ExtractedTenderV1(
        subject="строительство жилых домов",  # doesn't contain "99.99"
        bid_security_required=True,
        bid_security_amount=Decimal("50000"),  # 50000 > 100
        qualification_requirements=[
            "наличие СРО обязательно",
            "лицензия МЧС обязательна",
            "опыт выполнения аналогичных работ",
        ],
    )
    checklist = _checklist(sro_req=True, license_req=True, experience_req=True)
    result = scorer.score(profile, checklist, extracted)
    assert result.fit_score == 0.0
    assert result.components.okved is False
    assert result.components.sro is False
    assert result.components.license is False
    assert result.components.experience is False
    assert result.components.finance is False


# ── 3. All None → fit_score = 50 (neutral) ───────────────────────────────────

def test_none_components_neutral():
    # Empty profile, no bid_security_amount → all None
    profile = {}
    extracted = ExtractedTenderV1()  # no bid_security_amount
    checklist = _checklist()  # nothing required → sro/license/experience = True, not None
    # To get all None: don't set okved_main, bid_security=None already
    # sro/license/experience: not required → True (not None). Use required=True + no profile data
    checklist_all_req = _checklist(sro_req=True, license_req=True, experience_req=True)
    # profile has no sro/licenses/experience keys → None for each
    result = scorer.score(profile, checklist_all_req, extracted)
    assert result.fit_score == 50.0
    assert result.components.okved is None
    assert result.components.sro is None
    assert result.components.license is None
    assert result.components.experience is None
    assert result.components.finance is None


# ── 4. SRO not required → sro_ok = True ──────────────────────────────────────

def test_sro_not_required():
    profile = {}  # no SRO data
    extracted = ExtractedTenderV1()
    checklist = _checklist(sro_req=False)
    result = scorer.score(profile, checklist, extracted)
    assert result.components.sro is True


# ── 5. SRO required but company lacks it → sro_ok = False ────────────────────

def test_sro_required_missing():
    profile = {"sro": {"has_sro": False}}
    extracted = ExtractedTenderV1(
        qualification_requirements=["наличие СРО обязательно"]
    )
    checklist = _checklist(sro_req=True)
    result = scorer.score(profile, checklist, extracted)
    assert result.components.sro is False


# ── 6. Finance sufficient → funds_ok = True ──────────────────────────────────

def test_finance_sufficient():
    profile = {"financial": {"available_funds": 1_000_000}}
    extracted = ExtractedTenderV1(
        bid_security_required=True,
        bid_security_amount=Decimal("500000"),
    )
    result = scorer.score(profile, [], extracted)
    assert result.components.finance is True


# ── 7. Finance insufficient → funds_ok = False ───────────────────────────────

def test_finance_insufficient():
    profile = {"financial": {"available_funds": 10_000}}
    extracted = ExtractedTenderV1(
        bid_security_required=True,
        bid_security_amount=Decimal("500000"),
    )
    result = scorer.score(profile, [], extracted)
    assert result.components.finance is False


# ── 8. Mixed components → correct weighted aggregate ─────────────────────────

def test_aggregate_calculation():
    # okved=True(25) + sro=None(10) + license=True(20) + experience=True(20) + funds=False(0) = 75
    profile = {
        "okved_main": "41.20",
        "licenses": [{"active": True}],
        "experience": {"years": 3},
        "financial": {"available_funds": 100},  # less than bid_security
    }
    extracted = ExtractedTenderV1(
        subject="работы 41.20 по объекту",
        bid_security_required=True,
        bid_security_amount=Decimal("999999"),
        qualification_requirements=[
            "лицензия МЧС обязательна",
            "опыт выполнения аналогичных работ",
            "наличие СРО обязательно",
        ],
    )
    checklist = _checklist(sro_req=True, license_req=True, experience_req=True)
    result = scorer.score(profile, checklist, extracted)
    # sro required=True but no sro key in profile → None → 10 pts (50% of 20)
    assert result.components.okved is True
    assert result.components.sro is None
    assert result.components.license is True
    assert result.components.experience is True
    assert result.components.finance is False
    assert result.fit_score == 75.0
