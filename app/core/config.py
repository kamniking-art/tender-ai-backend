from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_url: str = "postgresql+asyncpg://postgres:postgres@tender_ai_db:5432/tender_ai"
    database_url_sync: str = "postgresql+psycopg2://postgres:postgres@tender_ai_db:5432/tender_ai"

    secret_key: str = "change_me_to_long_random_string"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    storage_root: str = "/data"
    documents_subdir: str = "tender_docs"
    task_sla_check_interval_minutes: int = 5


settings = Settings()
