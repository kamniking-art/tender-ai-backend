from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from app.relevance.service import compute_relevance_v2


class RelevanceV23UnitTests(TestCase):
    def _tender(self, title: str):
        return SimpleNamespace(
            title=title,
            customer_name="",
            region="",
            place_text="",
        )

    def test_household_food_noise_goes_irrelevant(self) -> None:
        result = compute_relevance_v2(
            tender=self._tender("Поставка мясорубки электрической для пищеблока"),
            analysis=None,
            extracted=None,
        )
        self.assertEqual(result["category"], "нерелевантно / прочее")
        self.assertLess(result["score"], 30)
        self.assertIn("бытовой/пищевой", result["reason"])

    def test_mixed_tile_and_food_context_is_suppressed(self) -> None:
        result = compute_relevance_v2(
            tender=self._tender("Поставка плитки для пищеблока и кухонного блока"),
            analysis=None,
            extracted=None,
        )
        self.assertEqual(result["category"], "нерелевантно / прочее")
        self.assertLess(result["score"], 35)

    def test_building_materials_stays_materials(self) -> None:
        result = compute_relevance_v2(
            tender=self._tender("Поставка керамогранита и бордюрного камня"),
            analysis=None,
            extracted=None,
        )
        self.assertEqual(result["category"], "строительные материалы")
        self.assertGreaterEqual(result["score"], 45)

    def test_household_equipment_context_overrides_generic_supply(self) -> None:
        result = compute_relevance_v2(
            tender=self._tender("Поставка оборудования для пищеблока (плиты электрические)"),
            analysis=None,
            extracted=None,
        )
        self.assertEqual(result["category"], "нерелевантно / прочее")
        self.assertLess(result["score"], 20)
