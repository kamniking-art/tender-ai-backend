"""Unit tests for FitScorer — Financial Profile v1 (economics_ok, risk_ok).

Pure function tests — no DB, no IO required.
"""
from __future__ import annotations

import pytest

from app.ai_extraction.schemas import ExtractedTenderV1
from app.fit_score.scorer import FitScorer

scorer = FitScorer()


def _extracted(margin_pct: float | None = None, risk_score: int | None = None) -> ExtractedTenderV1:
    e = ExtractedTenderV1(schema_version="v1")
    if margin_pct is not None:
        e._estimated_margin_pct = margin_pct
    if risk_score is not None:
        e._risk_score = risk_score
    return e


def _components(profile: dict, margin_pct: float | None = None, risk_score: int | None = None):
    return scorer.score(profile, [], _extracted(margin_pct, risk_score)).components


def _score(profile: dict, margin_pct: float | None = None, risk_score: int | None = None) -> float:
    return scorer.score(profile, [], _extracted(margin_pct, risk_score)).fit_score


# ── economics_ok ──────────────────────────────────────────────────────────────

class TestEconomics:
    def test_no_min_margin_configured_neutral(self):
        assert _components({}).economics_ok is None

    def test_margin_not_in_tender_neutral(self):
        profile = {"min_margin_percent": 15.0}
        assert _components(profile, margin_pct=None).economics_ok is None

    def test_margin_meets_minimum_true(self):
        profile = {"min_margin_percent": 15.0}
        assert _components(profile, margin_pct=20.0).economics_ok is True

    def test_margin_exactly_at_minimum_true(self):
        profile = {"min_margin_percent": 15.0}
        assert _components(profile, margin_pct=15.0).economics_ok is True

    def test_margin_below_minimum_false(self):
        profile = {"min_margin_percent": 15.0}
        assert _components(profile, margin_pct=10.0).economics_ok is False

    def test_margin_zero_below_minimum_false(self):
        profile = {"min_margin_percent": 5.0}
        assert _components(profile, margin_pct=0.0).economics_ok is False

    def test_margin_false_applies_penalty_20(self):
        base = _score({})
        penalized = _score({"min_margin_percent": 20.0}, margin_pct=5.0)
        assert penalized == pytest.approx(base - 20, abs=1)

    def test_margin_true_no_penalty(self):
        base = _score({})
        ok = _score({"min_margin_percent": 10.0}, margin_pct=25.0)
        assert ok == pytest.approx(base, abs=1)

    def test_margin_none_no_penalty(self):
        base = _score({})
        neutral = _score({"min_margin_percent": 10.0}, margin_pct=None)
        assert neutral == pytest.approx(base, abs=1)


# ── risk_ok ───────────────────────────────────────────────────────────────────

class TestRisk:
    def test_no_risk_tolerance_neutral(self):
        assert _components({}, risk_score=80).risk_ok is None

    def test_risk_score_not_in_tender_neutral(self):
        profile = {"risk_tolerance": "medium"}
        assert _components(profile, risk_score=None).risk_ok is None

    def test_low_within_threshold_true(self):
        profile = {"risk_tolerance": "low"}
        assert _components(profile, risk_score=25).risk_ok is True

    def test_low_at_threshold_true(self):
        profile = {"risk_tolerance": "low"}
        assert _components(profile, risk_score=30).risk_ok is True

    def test_low_exceeds_threshold_false(self):
        profile = {"risk_tolerance": "low"}
        assert _components(profile, risk_score=31).risk_ok is False

    def test_medium_within_threshold_true(self):
        profile = {"risk_tolerance": "medium"}
        assert _components(profile, risk_score=60).risk_ok is True

    def test_medium_exceeds_threshold_false(self):
        profile = {"risk_tolerance": "medium"}
        assert _components(profile, risk_score=61).risk_ok is False

    def test_high_always_true_for_any_score(self):
        profile = {"risk_tolerance": "high"}
        assert _components(profile, risk_score=100).risk_ok is True
        assert _components(profile, risk_score=99).risk_ok is True

    def test_risk_false_applies_penalty_20(self):
        base = _score({})
        penalized = _score({"risk_tolerance": "low"}, risk_score=50)
        assert penalized == pytest.approx(base - 20, abs=1)

    def test_risk_true_no_penalty(self):
        base = _score({})
        ok = _score({"risk_tolerance": "medium"}, risk_score=40)
        assert ok == pytest.approx(base, abs=1)

    def test_risk_none_no_penalty(self):
        base = _score({})
        neutral = _score({"risk_tolerance": "medium"}, risk_score=None)
        assert neutral == pytest.approx(base, abs=1)

    def test_invalid_tolerance_neutral(self):
        profile = {"risk_tolerance": "ultra"}
        assert _components(profile, risk_score=50).risk_ok is None


# ── combined penalties ────────────────────────────────────────────────────────

class TestCombinedPenalties:
    def test_both_false_stacks_to_40(self):
        base = _score({})
        both = _score(
            {"min_margin_percent": 20.0, "risk_tolerance": "low"},
            margin_pct=5.0,
            risk_score=50,
        )
        assert both == pytest.approx(base - 40, abs=1)

    def test_penalties_floor_at_zero(self):
        result = scorer.score(
            {
                "min_margin_percent": 50.0,
                "risk_tolerance": "low",
                "service_regions": ["Москва"],
                "min_nmck": 10_000_000,
                "max_active_projects": 1,
                "active_projects_count": 5,
            },
            [],
            _extracted(margin_pct=1.0, risk_score=90),
        )
        assert result.fit_score >= 0.0

    def test_none_fields_no_penalty(self):
        """All financial fields None → same score as empty profile."""
        base = _score({})
        no_data = _score({"min_margin_percent": 15.0, "risk_tolerance": "medium"})
        assert no_data == pytest.approx(base, abs=1)
