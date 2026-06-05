"""Unit tests for FitScorer — Business Profile v1 (service_regions, nmck range).

Pure function tests — no DB, no IO required.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.ai_extraction.schemas import ExtractedTenderV1
from app.fit_score.scorer import FitScorer

scorer = FitScorer()

# ── helpers ───────────────────────────────────────────────────────────────────

def _extracted(nmck: float | None = None, region: str | None = None) -> ExtractedTenderV1:
    e = ExtractedTenderV1(
        schema_version="v1",
        nmck=Decimal(str(nmck)) if nmck is not None else None,
    )
    if region:
        e._tender_region = region
    return e


def _score(profile: dict, nmck: float | None = None, region: str | None = None) -> float:
    return scorer.score(profile, [], _extracted(nmck=nmck, region=region)).fit_score


def _components(profile: dict, nmck: float | None = None, region: str | None = None):
    return scorer.score(profile, [], _extracted(nmck=nmck, region=region)).components


# ── service_regions ───────────────────────────────────────────────────────────

class TestServiceRegions:
    def test_no_service_regions_configured_neutral(self):
        """No service_regions → region_ok=None, no penalty."""
        c = _components({}, region="Москва")
        assert c.region_ok is None

    def test_region_in_list_true(self):
        profile = {"service_regions": ["Москва", "Санкт-Петербург"]}
        c = _components(profile, region="Москва")
        assert c.region_ok is True

    def test_region_partial_match_true(self):
        """Partial substring match — 'Воронежская область' contains 'Воронеж'."""
        profile = {"service_regions": ["Воронеж"]}
        c = _components(profile, region="Воронежская область")
        assert c.region_ok is True

    def test_region_not_in_list_false(self):
        profile = {"service_regions": ["Москва", "Санкт-Петербург"]}
        c = _components(profile, region="Новосибирск")
        assert c.region_ok is False

    def test_region_mismatch_applies_penalty(self):
        """region_ok=False → fit_score reduced by 15."""
        base = _score({})  # no service_regions
        penalized = _score({"service_regions": ["Москва"]}, region="Новосибирск")
        assert penalized == pytest.approx(base - 15, abs=1)

    def test_region_match_no_penalty(self):
        """region_ok=True → no penalty vs no service_regions configured."""
        base = _score({})
        matched = _score({"service_regions": ["Москва"]}, region="Москва")
        assert matched == pytest.approx(base, abs=1)

    def test_no_region_data_neutral(self):
        """service_regions configured but no tender region → neutral, no penalty."""
        profile = {"service_regions": ["Москва"]}
        c = _components(profile, region=None)
        assert c.region_ok is None

    def test_case_insensitive_match(self):
        profile = {"service_regions": ["москва"]}
        c = _components(profile, region="Москва")
        assert c.region_ok is True


# ── nmck_range ────────────────────────────────────────────────────────────────

class TestNmckRange:
    def test_no_range_configured_neutral(self):
        c = _components({}, nmck=500_000)
        assert c.nmck_range_ok is None

    def test_nmck_in_range_true(self):
        profile = {"min_nmck": 100_000, "max_nmck": 5_000_000}
        c = _components(profile, nmck=500_000)
        assert c.nmck_range_ok is True

    def test_nmck_below_min_false(self):
        profile = {"min_nmck": 100_000, "max_nmck": 5_000_000}
        c = _components(profile, nmck=50_000)
        assert c.nmck_range_ok is False

    def test_nmck_above_max_false(self):
        profile = {"min_nmck": 100_000, "max_nmck": 5_000_000}
        c = _components(profile, nmck=10_000_000)
        assert c.nmck_range_ok is False

    def test_nmck_at_boundary_min_true(self):
        profile = {"min_nmck": 100_000}
        c = _components(profile, nmck=100_000)
        assert c.nmck_range_ok is True

    def test_nmck_at_boundary_max_true(self):
        profile = {"max_nmck": 5_000_000}
        c = _components(profile, nmck=5_000_000)
        assert c.nmck_range_ok is True

    def test_only_min_configured(self):
        """Only min_nmck set — max=None means no upper bound."""
        profile = {"min_nmck": 100_000}
        assert _components(profile, nmck=99_999).nmck_range_ok is False
        assert _components(profile, nmck=999_999_999).nmck_range_ok is True

    def test_only_max_configured(self):
        """Only max_nmck set — min=None means no lower bound."""
        profile = {"max_nmck": 1_000_000}
        assert _components(profile, nmck=1_000_001).nmck_range_ok is False
        assert _components(profile, nmck=1).nmck_range_ok is True

    def test_nmck_out_of_range_applies_penalty(self):
        base = _score({})
        penalized = _score({"min_nmck": 1_000_000}, nmck=100)
        assert penalized == pytest.approx(base - 15, abs=1)

    def test_no_nmck_neutral(self):
        """NMCK not in tender → neutral, no penalty."""
        profile = {"min_nmck": 100_000, "max_nmck": 5_000_000}
        c = _components(profile, nmck=None)
        assert c.nmck_range_ok is None

    def test_both_penalties_stack(self):
        """region mismatch + nmck out of range → -30 total."""
        base = _score({})
        both = _score(
            {"service_regions": ["Москва"], "min_nmck": 1_000_000},
            nmck=100,
            region="Новосибирск",
        )
        assert both == pytest.approx(base - 30, abs=1)

    def test_penalties_floor_at_zero(self):
        """Score never goes negative."""
        result = scorer.score(
            {"service_regions": ["Москва"], "min_nmck": 1_000_000},
            [],
            _extracted(nmck=100, region="Новосибирск"),
        )
        assert result.fit_score >= 0.0
