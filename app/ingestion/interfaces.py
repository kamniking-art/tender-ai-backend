from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass
class IngestionRunStats:
    pages: int
    candidates_total: int
    inserted_count: int
    updated_count: int
    skipped_count: int


class IngestionProvider(Protocol):
    async def run_for_company(self, company_id: UUID, settings: dict) -> IngestionRunStats: ...
