from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class EISOpenDataDiscoveryState(BaseModel):
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    cooldown_until: datetime | None = None
    search_api_url: str | None = None
    dataset_api_url: str | None = None
    status: Literal["unknown", "maintenance", "ok"] = "unknown"
    last_error: str | None = None


class EISOpenDataDatasetState(BaseModel):
    last_processed_version: str | None = None
    last_processed_at: datetime | None = None
    last_processed_file: str | None = None


class EISOpenDataState(BaseModel):
    datasets: dict[str, EISOpenDataDatasetState] = Field(default_factory=dict)
    discovery: EISOpenDataDiscoveryState = Field(default_factory=EISOpenDataDiscoveryState)


class EISOpenDataSettings(BaseModel):
    enabled: bool = False
    interval_minutes: int = 60
    dataset_ids: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=lambda: ["гранит", "памятник", "плита", "надгроб"])
    regions: list[str] = Field(default_factory=list)
    laws: list[str] = Field(default_factory=list)
    max_files_per_run: int = 2
    max_records_per_file: int = 20_000
    download_timeout_sec: int = 60
    rate_limit_rps: float = 0.2
    storage_dir: str = "/data/opendata_cache"
    allow_demo: bool = False
    state: EISOpenDataState = Field(default_factory=EISOpenDataState)


class IngestionSettingsPatch(BaseModel):
    eis_public: dict | None = None
    eis_opendata: EISOpenDataSettings | None = None


class EISDatasetFile(BaseModel):
    name: str | None = None
    url: str
    updated_at: datetime | None = None
    size: int | None = None
    format: str | None = None


class EISDatasetSummary(BaseModel):
    dataset_id: str
    title: str | None = None
    updated_at: datetime | None = None
    files: list[EISDatasetFile] = Field(default_factory=list)


@dataclass
class DatasetResource:
    url: str
    name: str | None = None
    updated_at: datetime | None = None
    version: str | None = None
    size: int | None = None
    format: str | None = None


@dataclass
class DatasetMeta:
    dataset_id: str
    title: str | None = None
    updated_at: datetime | None = None
    resources: list[DatasetResource] = field(default_factory=list)


@dataclass
class OpenDataCandidate:
    external_id: str
    title: str | None = None
    customer_name: str | None = None
    region: str | None = None
    procurement_type: str | None = None
    nmck: Decimal | None = None
    published_at: datetime | None = None
    submission_deadline: datetime | None = None


@dataclass
class DiscoveryResult:
    status: Literal["unknown", "maintenance", "ok"]
    search_api_url: str | None = None
    dataset_api_url: str | None = None
    last_error: str | None = None
    catalog_url: str | None = None
    http_status: int | None = None


@dataclass
class ProbeResult:
    ok: bool
    status: Literal["unknown", "maintenance", "ok"]
    last_error: str | None = None
    http_status: int | None = None
