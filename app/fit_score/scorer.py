"""FitScorer — deterministic company ↔ tender fit calculation.

Rules:
  • Pure function — no IO, no LLM, no DB.
  • aggregate fit_score = sum(component_score × weight), range 0–100.
  • None component → contributes 50 % of its weight (neutral / unknown).

Component weights (core):
  okved      25 %
  sro        20 %
  license    20 %
  experience 20 %
  finance    15 %

Business profile v1 (additive penalty only, does not inflate score):
  region_ok    — if False: -15 pts from final score
  nmck_range_ok — if False: -15 pts from final score
"""
from __future__ import annotations

from decimal import Decimal

from app.ai_extraction.schemas import ExtractedTenderV1
from app.fit_score.schema import FitScoreComponents, FitScoreResult
from app.requirements.schema import NormalizedRequirement, RequirementType

# (component_name, weight)
_WEIGHTS: dict[str, float] = {
    "okved":      25.0,
    "sro":        20.0,
    "license":    20.0,
    "experience": 20.0,
    "finance":    15.0,
}


def _component_points(value: bool | None, weight: float, *, none_factor: float = 0.5) -> float:
    """Convert bool | None to weighted points."""
    if value is True:
        return weight
    if value is False:
        return 0.0
    return weight * none_factor  # None → neutral (caller can override)


def _checklist_required(
    checklist: list[NormalizedRequirement],
    req_type: RequirementType,
) -> bool:
    """Return True if this requirement type is marked required in the checklist."""
    for item in checklist:
        if item.canonical_type == req_type:
            return item.required
    return False  # not found → treat as not required


class FitScorer:
    def score(
        self,
        profile: dict,
        checklist: list[NormalizedRequirement],
        extracted: ExtractedTenderV1,
    ) -> FitScoreResult:
        """Compute fit score for the company against a tender.

        Args:
            profile:   companies.profile JSONB dict.
            checklist: NormalizedRequirement list from RequirementNormalizer.
            extracted: ExtractedTenderV1 from AI extraction.

        Returns:
            FitScoreResult with per-component flags and aggregate fit_score.
        """
        okved      = self._okved(profile, extracted)
        sro        = self._sro(profile, checklist)
        license_ok = self._license(profile, checklist)
        experience = self._experience(profile, checklist)
        finance    = self._finance(profile, extracted)
        region_ok  = self._region(profile, extracted)
        nmck_ok    = self._nmck_range(profile, extracted)

        fit_score = (
            # okved=None means profile has no okved_main → conservative 20% (not neutral 50%)
            # because unknown OKVED is closer to "unconfirmed" than "neutral"
            _component_points(okved,      _WEIGHTS["okved"], none_factor=0.2)
            + _component_points(sro,      _WEIGHTS["sro"])
            + _component_points(license_ok, _WEIGHTS["license"])
            + _component_points(experience, _WEIGHTS["experience"])
            + _component_points(finance,  _WEIGHTS["finance"])
        )

        # Business profile v1 — penalty only, does not inflate score above base
        if region_ok is False:
            fit_score = max(0.0, fit_score - 15.0)
        if nmck_ok is False:
            fit_score = max(0.0, fit_score - 15.0)

        return FitScoreResult(
            components=FitScoreComponents(
                okved=okved,
                sro=sro,
                license=license_ok,
                experience=experience,
                finance=finance,
                region_ok=region_ok,
                nmck_range_ok=nmck_ok,
            ),
            fit_score=round(fit_score, 2),
        )

    # ── Component calculators ─────────────────────────────────────────────────

    def _okved(self, profile: dict, extracted: ExtractedTenderV1) -> bool | None:
        okved_main: str | None = profile.get("okved_main")
        if not okved_main:
            return None  # no OKVED in profile → unknown

        okved_lower = okved_main.lower()

        # Check tender subject
        subject = (extracted.subject or "").lower()
        if okved_lower in subject:
            return True

        # Check qualification_requirements
        for req in extracted.qualification_requirements:
            if okved_lower in req.lower():
                return True

        # Check additional OKVEDs in profile against tender text
        tender_text = subject + " " + " ".join(extracted.qualification_requirements).lower()
        okved_additional: list[str] = profile.get("okved_additional") or []
        for extra in okved_additional:
            if extra.lower() in tender_text:
                return True

        return False

    def _sro(
        self, profile: dict, checklist: list[NormalizedRequirement]
    ) -> bool | None:
        required = _checklist_required(checklist, RequirementType.SRO)
        if not required:
            return True  # tender doesn't require SRO → ok

        has_sro: bool | None = (profile.get("sro") or {}).get("has_sro")
        if has_sro is True:
            return True
        if has_sro is False:
            return False
        return None  # no data

    def _license(
        self, profile: dict, checklist: list[NormalizedRequirement]
    ) -> bool | None:
        required = _checklist_required(checklist, RequirementType.LICENSE)
        if not required:
            return True

        licenses: list[dict] = profile.get("licenses") or []
        if not licenses:
            return None  # no license data at all

        has_active = any(item.get("active") is True for item in licenses)
        return True if has_active else False

    def _experience(
        self, profile: dict, checklist: list[NormalizedRequirement]
    ) -> bool | None:
        required = _checklist_required(checklist, RequirementType.EXPERIENCE)
        if not required:
            return True

        # Key not in profile at all → unknown
        if "experience" not in profile:
            return None

        experience = profile["experience"]

        # Empty dict → False (explicitly set but empty)
        if not experience:
            return False

        # Non-empty → True (company has some experience data)
        return True

    def _finance(
        self, profile: dict, extracted: ExtractedTenderV1
    ) -> bool | None:
        bid_amount: Decimal | None = extracted.bid_security_amount
        if bid_amount is None:
            return None  # no financial requirement to check against

        available = (profile.get("financial") or {}).get("available_funds")
        if available is None:
            return None

        try:
            return Decimal(str(available)) >= bid_amount
        except Exception:
            return None

    # ── Business profile v1 ───────────────────────────────────────────────────

    def _region(self, profile: dict, extracted: ExtractedTenderV1) -> bool | None:
        """Return True if tender region is within company service_regions.

        Returns None if service_regions is not configured (no penalty).
        Returns False if configured and tender region doesn't match any entry.
        """
        service_regions: list[str] = profile.get("service_regions") or []
        if not service_regions:
            return None  # not configured → neutral

        # ExtractedTenderV1 doesn't carry region — we check via tender.region
        # which is not available here. Signal is injected by caller when available.
        # When called without tender context, fall back to None (neutral).
        tender_region: str | None = getattr(extracted, "_tender_region", None)
        if not tender_region:
            return None  # no region data to match

        tender_region_lower = tender_region.lower()
        for r in service_regions:
            if r.lower() in tender_region_lower or tender_region_lower in r.lower():
                return True
        return False

    def _nmck_range(self, profile: dict, extracted: ExtractedTenderV1) -> bool | None:
        """Return True if tender NMCK is within company [min_nmck, max_nmck].

        Returns None if neither bound is configured (no penalty).
        Returns False if configured and NMCK is out of range.
        """
        min_nmck = profile.get("min_nmck")
        max_nmck = profile.get("max_nmck")
        if min_nmck is None and max_nmck is None:
            return None  # not configured → neutral

        nmck: Decimal | None = extracted.nmck
        if nmck is None:
            return None  # no NMCK to check

        try:
            nmck_dec = Decimal(str(nmck))
            if min_nmck is not None and nmck_dec < Decimal(str(min_nmck)):
                return False
            if max_nmck is not None and nmck_dec > Decimal(str(max_nmck)):
                return False
            return True
        except Exception:
            return None
