"""Unit tests for RequirementNormalizer.

Pure function tests — no DB, no IO required.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.ai_extraction.schemas import ExtractedTenderV1
from app.requirements.normalizer import RequirementNormalizer
from app.requirements.schema import RequirementType

normalizer = RequirementNormalizer()

ALL_TYPES = set(RequirementType)


# ── 1. bid_security detected ──────────────────────────────────────────────────

def test_bid_security_detected():
    extracted = ExtractedTenderV1(bid_security_required=True, bid_security_amount=Decimal("50000"))
    results = normalizer.normalize(extracted)
    bid = next(r for r in results if r.canonical_type == RequirementType.BID_SECURITY)
    assert bid.required is True
    assert bid.status == "ok"


# ── 2. sro detected from qualification_requirements ──────────────────────────

def test_sro_detected_from_qualification():
    extracted = ExtractedTenderV1(
        qualification_requirements=["наличие СРО обязательно"]
    )
    results = normalizer.normalize(extracted)
    sro = next(r for r in results if r.canonical_type == RequirementType.SRO)
    assert sro.required is True
    assert sro.status == "ok"
    assert sro.evidence is not None


# ── 3. license detected ───────────────────────────────────────────────────────

def test_license_detected():
    extracted = ExtractedTenderV1(
        qualification_requirements=["лицензия МЧС обязательна"]
    )
    results = normalizer.normalize(extracted)
    lic = next(r for r in results if r.canonical_type == RequirementType.LICENSE)
    assert lic.required is True
    assert lic.status == "ok"


# ── 4. experience detected ────────────────────────────────────────────────────

def test_experience_detected():
    extracted = ExtractedTenderV1(
        qualification_requirements=["опыт не менее 3 лет выполнения аналогичных работ"]
    )
    results = normalizer.normalize(extracted)
    exp = next(r for r in results if r.canonical_type == RequirementType.EXPERIENCE)
    assert exp.required is True
    assert exp.status == "ok"


# ── 5. empty extraction → 7 requirements, all status=unknown ─────────────────

def test_empty_extraction():
    extracted = ExtractedTenderV1()
    results = normalizer.normalize(extracted)
    assert len(results) == 7
    for r in results:
        assert r.status == "unknown", f"{r.canonical_type} expected unknown, got {r.status}"


# ── 6. no LLM / no IO in normalizer ──────────────────────────────────────────

def test_no_lm_in_normalizer():
    import sys
    # Verify that normalizer module does not import any LLM client packages
    normalizer_module = sys.modules.get("app.requirements.normalizer")
    assert normalizer_module is not None
    source = getattr(normalizer_module, "__file__", "")
    # Simply calling normalize() must not trigger any network or LLM import
    extracted = ExtractedTenderV1(qualification_requirements=["опыт 5 лет"])
    result = normalizer.normalize(extracted)
    assert len(result) == 7
    # Spot-check no LLM client loaded into this test's sys.modules
    llm_modules = [m for m in sys.modules if "anthropic" in m or "openai" in m]
    assert llm_modules == [], f"LLM modules unexpectedly imported: {llm_modules}"


# ── 7. all canonical types always present ────────────────────────────────────

def test_all_canonical_types_present():
    extracted = ExtractedTenderV1()
    results = normalizer.normalize(extracted)
    returned_types = {r.canonical_type for r in results}
    assert returned_types == ALL_TYPES


# ── 8. bank_guarantee detected ───────────────────────────────────────────────

def test_bank_guarantee_detected():
    extracted = ExtractedTenderV1(
        qualification_requirements=["требуется банковская гарантия на исполнение контракта"]
    )
    results = normalizer.normalize(extracted)
    bg = next(r for r in results if r.canonical_type == RequirementType.BANK_GUARANTEE)
    assert bg.required is True
    assert bg.status == "ok"
    assert bg.evidence is not None
