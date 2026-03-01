from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentPackageGenerateRequest(BaseModel):
    force: bool = False


class GeneratedFileRead(BaseModel):
    document_id: UUID
    filename: str


class DocumentPackageGenerateResponse(BaseModel):
    ok: bool = True
    generated_files: list[GeneratedFileRead]
    checklist: list[str]


class PackageFileRead(BaseModel):
    document_id: UUID
    filename: str
    content_type: str | None
    file_size: int | None
    uploaded_at: datetime


class DocumentPackageReadResponse(BaseModel):
    exists: bool
    files: list[PackageFileRead] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
