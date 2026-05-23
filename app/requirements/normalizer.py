"""RequirementNormalizer — deterministic mapping from ExtractedTenderV1 to
a fixed list of NormalizedRequirement objects.

Rules:
  • Always returns exactly 7 NormalizedRequirement items (one per RequirementType).
  • Pure function — no IO, no LLM, no DB.
  • Never raises — unknown/missing data yields status="unknown", required=False.
"""
from __future__ import annotations

from app.ai_extraction.schemas import ExtractedTenderV1
from app.requirements.schema import NormalizedRequirement, RequirementType


class RequirementNormalizer:
    # ── keyword sets ─────────────────────────────────────────────────────────
    _SRO_KEYWORDS = ("сро", "саморегулируемая", "допуск")
    _LICENSE_KEYWORDS = ("лицензия", "лицензии", "мчс", "фсб", "росатом")
    _EXPERIENCE_KEYWORDS = ("опыт", "контракт", "лет", "выполнен", "выполненных")
    _BANK_GUARANTEE_KEYWORDS = ("банковская гарантия", "гарантия исполнения")
    _TIMELINE_KEYWORDS = ("срок", "дней", "месяц", "выполнения")

    # ── public API ────────────────────────────────────────────────────────────

    def normalize(self, extracted: ExtractedTenderV1) -> list[NormalizedRequirement]:
        """Map extracted tender data to the canonical 7-type checklist.

        Always returns all 7 types in RequirementType definition order.
        """
        return [
            self._bid_security(extracted),
            self._contract_security(extracted),
            self._from_qualifications(extracted, RequirementType.SRO, self._SRO_KEYWORDS),
            self._from_qualifications(extracted, RequirementType.LICENSE, self._LICENSE_KEYWORDS),
            self._from_qualifications(extracted, RequirementType.EXPERIENCE, self._EXPERIENCE_KEYWORDS),
            self._from_qualifications_multi(
                extracted, RequirementType.BANK_GUARANTEE, self._BANK_GUARANTEE_KEYWORDS
            ),
            self._from_tech_parameters(extracted),
        ]

    # ── private helpers ───────────────────────────────────────────────────────

    def _bid_security(self, extracted: ExtractedTenderV1) -> NormalizedRequirement:
        required = extracted.bid_security_required is True
        if required:
            has_detail = (
                extracted.bid_security_amount is not None
                or extracted.bid_security_pct is not None
            )
            status = "ok" if has_detail else "unknown"
            parts: list[str] = []
            if extracted.bid_security_amount is not None:
                parts.append(f"Сумма: {extracted.bid_security_amount}")
            if extracted.bid_security_pct is not None:
                parts.append(f"{extracted.bid_security_pct}%")
            evidence = ", ".join(parts) or None
        else:
            status = "unknown"
            evidence = None
        return NormalizedRequirement(
            canonical_type=RequirementType.BID_SECURITY,
            required=required,
            status=status,
            evidence=evidence,
        )

    def _contract_security(self, extracted: ExtractedTenderV1) -> NormalizedRequirement:
        required = extracted.contract_security_required is True
        if required:
            has_detail = (
                extracted.contract_security_amount is not None
                or extracted.contract_security_pct is not None
            )
            status = "ok" if has_detail else "unknown"
            parts: list[str] = []
            if extracted.contract_security_amount is not None:
                parts.append(f"Сумма: {extracted.contract_security_amount}")
            if extracted.contract_security_pct is not None:
                parts.append(f"{extracted.contract_security_pct}%")
            evidence = ", ".join(parts) or None
        else:
            status = "unknown"
            evidence = None
        return NormalizedRequirement(
            canonical_type=RequirementType.CONTRACT_SECURITY,
            required=required,
            status=status,
            evidence=evidence,
        )

    def _from_qualifications(
        self,
        extracted: ExtractedTenderV1,
        req_type: RequirementType,
        keywords: tuple[str, ...],
    ) -> NormalizedRequirement:
        """Match single-word keywords against qualification_requirements lines."""
        matched: str | None = None
        for item in extracted.qualification_requirements:
            lower = item.lower()
            if any(kw in lower for kw in keywords):
                matched = item
                break
        if matched:
            return NormalizedRequirement(
                canonical_type=req_type,
                required=True,
                status="ok",
                evidence=matched,
            )
        return NormalizedRequirement(
            canonical_type=req_type,
            required=False,
            status="unknown",
            evidence=None,
        )

    def _from_qualifications_multi(
        self,
        extracted: ExtractedTenderV1,
        req_type: RequirementType,
        phrases: tuple[str, ...],
    ) -> NormalizedRequirement:
        """Match multi-word phrases (substring) against qualification_requirements lines."""
        matched: str | None = None
        for item in extracted.qualification_requirements:
            lower = item.lower()
            if any(phrase in lower for phrase in phrases):
                matched = item
                break
        if matched:
            return NormalizedRequirement(
                canonical_type=req_type,
                required=True,
                status="ok",
                evidence=matched,
            )
        return NormalizedRequirement(
            canonical_type=req_type,
            required=False,
            status="unknown",
            evidence=None,
        )

    def _from_tech_parameters(self, extracted: ExtractedTenderV1) -> NormalizedRequirement:
        """Match execution_timeline keywords against tech_parameters."""
        matched: str | None = None
        for item in extracted.tech_parameters:
            lower = item.lower()
            if any(kw in lower for kw in self._TIMELINE_KEYWORDS):
                matched = item
                break
        if matched:
            return NormalizedRequirement(
                canonical_type=RequirementType.EXECUTION_TIMELINE,
                required=True,
                status="ok",
                evidence=matched,
            )
        return NormalizedRequirement(
            canonical_type=RequirementType.EXECUTION_TIMELINE,
            required=False,
            status="unknown",
            evidence=None,
        )
