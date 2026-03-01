from __future__ import annotations

from datetime import UTC, datetime

from app.ingestion.eis_opendata.schemas import DatasetResource, EISOpenDataDatasetState, EISOpenDataSettings


def get_dataset_state(settings: EISOpenDataSettings, dataset_id: str) -> EISOpenDataDatasetState:
    return settings.state.datasets.get(dataset_id, EISOpenDataDatasetState())


def build_resource_version(resource: DatasetResource) -> str:
    if resource.version:
        return resource.version
    if resource.updated_at:
        return resource.updated_at.isoformat()
    return resource.url


def should_process_resource(
    settings: EISOpenDataSettings,
    dataset_id: str,
    resource: DatasetResource,
) -> bool:
    dataset_state = get_dataset_state(settings, dataset_id)
    return build_resource_version(resource) != (dataset_state.last_processed_version or "")


def mark_dataset_processed(
    settings: EISOpenDataSettings,
    dataset_id: str,
    resource: DatasetResource,
) -> None:
    settings.state.datasets[dataset_id] = EISOpenDataDatasetState(
        last_processed_version=build_resource_version(resource),
        last_processed_at=datetime.now(UTC),
        last_processed_file=resource.url,
    )
