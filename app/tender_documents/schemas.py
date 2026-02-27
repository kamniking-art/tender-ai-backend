from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TenderDocumentRead(BaseModel):
    id: UUID
    tender_id: UUID
    file_name: str
    content_type: str | None
    doc_type: str | None
    file_size: int | None
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)
