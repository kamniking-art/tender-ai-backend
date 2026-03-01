from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from app.ai_extraction.schemas import ExtractedTenderV1

ExtractorErrorCode = Literal[
    "NO_DOCS",
    "UNSUPPORTED_FORMAT",
    "PROVIDER_TIMEOUT",
    "PROVIDER_ERROR",
    "VALIDATION_ERROR",
]


class ExtractionProviderError(RuntimeError):
    def __init__(self, code: ExtractorErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ExtractionProviderResult:
    extracted: ExtractedTenderV1
    extract_meta: dict


class ExtractionProvider(Protocol):
    async def extract(
        self,
        *,
        tender_id: UUID,
        company_id: UUID,
        tender_context: dict,
        text: str,
    ) -> ExtractionProviderResult: ...
