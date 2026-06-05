"""Unit tests for FitScorer — Capacity Profile v1 + region work_all_regions.

Pure function tests — no DB, no IO required.
"""
from __future__ import annotations

import pytest

from app.ai_extraction.schemas import ExtractedTenderV1
from app.fit_score.scorer import FitScorer

scorer = FitScorer()


def _extracted(region: str | None = None) -> ExtractedTenderV1:
    e = ExtractedTenderV1(schema_version="v1")
    if region:
        e._tender_region = region
    return e


def _components(profile: dict, region: str | None = None):
    return scorer.score(profile, [], _extracted(region=region)).components


def _score(profile: dict, region: str | None = None) -> float:
    return scorer.score(profile, [], _extracted(region=region)).fit_score


# ── capacity_ok ────────────────────────────────────────────────────────────────

class TestCapacity:
    def test_both_missing_neutral(self):
        """Neither field configured → capacity_ok=None, no penalty."""
        c = _components({})
        assert c.capacity_ok is None

    def test_only_max_missing_active_neutral(self):
        """max_active_projects set but active_projects_count absent → None."""
        c = _components({"max_active_projects": 5})
        assert c.capacity_ok is None

    def test_only_active_missing_max_neutral(self):
        """active_projects_count set but max_active_projects absent → None."""
        c = _components({"active_projects_count": 3})
        assert c.capacity_ok is None

    def test_within_capacity_true(self):
        profile = {"max_active_projects": 5, "active_projects_count": 3}
        assert _components(profile).capacity_ok is True

    def test_at_max_capacity_true(self):
        """Equal to max is OK (<=)."""
        profile = {"max_active_projects": 5, "active_projects_count": 5}
        assert _components(profile).capacity_ok is True

    def test_over_capacity_false(self):
        profile = {"max_active_projects": 5, "active_projects_count": 6}
        assert _components(profile).capacity_ok is False

    def test_zero_active_true(self):
        profile = {"max_active_projects": 3, "active_projects_count": 0}
        assert _components(profile).capacity_ok is True

    def test_capacity_false_applies_penalty(self):
        base = _score({})
        over = _score({"max_active_projects": 2, "active_projects_count": 5})
        assert over == pytest.approx(base - 15, abs=1)

    def test_capacity_true_no_penalty(self):
        base = _score({})
        ok = _score({"max_active_projects": 5, "active_projects_count": 3})
        assert ok == pytest.approx(base, abs=1)

    def test_capacity_none_no_penalty(self):
        """Unconfigured capacity → no penalty."""
        base = _score({})
        neutral = _score({"max_active_projects": 5})  # active_count missing
        assert neutral == pytest.approx(base, abs=1)

    def test_penalty_floor_at_zero(self):
        """Score never negative even with capacity + other penalties."""
        result = scorer.score(
            {
                "max_active_projects": 1,
                "active_projects_count": 99,
                "service_regions": ["Москва"],
                "min_nmck": 10_000_000,
            },
            [],
            _extracted(region="Новосибирск"),
        )
        assert result.fit_score >= 0.0


# ── work_all_regions ───────────────────────────────────────────────────────────

class TestWorkAllRegions:
    def test_work_all_regions_true_bypasses_check(self):
        """work_all_regions=True → region_ok=True regardless of service_regions."""
        profile = {"work_all_regions": True, "service_regions": ["Москва"]}
        c = _components(profile, region="Новосибирск")
        assert c.region_ok is True

    def test_work_all_regions_true_no_penalty(self):
        """work_all_regions=True → no region penalty even for mismatched region."""
        base = _score({})
        all_regions = _score(
            {"work_all_regions": True, "service_regions": ["Москва"]},
            region="Новосибирск",
        )
        assert all_regions == pytest.approx(base, abs=1)

    def test_work_all_regions_false_uses_service_regions(self):
        """work_all_regions=False → normal service_regions check."""
        profile = {"work_all_regions": False, "service_regions": ["Москва"]}
        c = _components(profile, region="Новосибирск")
        assert c.region_ok is False

    def test_work_all_regions_false_match_true(self):
        profile = {"work_all_regions": False, "service_regions": ["Москва"]}
        c = _components(profile, region="Москва")
        assert c.region_ok is True

    def test_empty_service_regions_neutral(self):
        """service_regions=[] (no work_all_regions) → None, no penalty."""
        profile = {"service_regions": []}
        c = _components(profile, region="Новосибирск")
        assert c.region_ok is None

    def test_work_all_regions_true_empty_list_true(self):
        """work_all_regions=True with empty list → True."""
        profile = {"work_all_regions": True, "service_regions": []}
        c = _components(profile, region="Новосибирск")
        assert c.region_ok is True

    def test_work_all_regions_not_set_falls_back(self):
        """work_all_regions absent → normal logic."""
        profile = {"service_regions": ["Москва"]}
        c = _components(profile, region="Новосибирск")
        assert c.region_ok is False
