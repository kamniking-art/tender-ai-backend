from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_version: str = "unknown"
    app_built_at: str = "unknown"
    public_base_url: str = "http://127.0.0.1:8000"
    web_cookie_secure: bool = False

    database_url: str = "postgresql+asyncpg://postgres:postgres@tender_ai_db:5432/tender_ai"
    database_url_sync: str = "postgresql+psycopg2://postgres:postgres@tender_ai_db:5432/tender_ai"

    secret_key: str = "change_me_to_long_random_string"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    storage_root: str = "/data"
    documents_subdir: str = "tender_docs"
    task_sla_check_interval_minutes: int = 5
    telegram_notify_interval_minutes: int = 5

    # EIS OpenData discovery/config
    eis_opendata_base_url: str = "https://zakupki.gov.ru"
    eis_opendata_search_path: str = "/epz/opendata/search/results.html"
    eis_opendata_search_api_url: str | None = None
    eis_opendata_dataset_api_url: str | None = None

    ai_extractor_base_url: str | None = None
    ai_extractor_api_key: str | None = None
    ai_extractor_timeout_sec: int = 60
    ai_extractor_max_chars: int = 120000
    ai_extractor_mode: str = "mock"
    auth_disabled: str = "false"
    ingestion_run_once_cooldown_minutes: int = 10

    @property
    def auth_disabled_enabled(self) -> bool:
        return str(self.auth_disabled).strip().lower() in {"1", "true", "yes", "on"}


settings = Settings()
