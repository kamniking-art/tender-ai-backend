import os
import logging
from datetime import datetime, timezone

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Response, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import AsyncSessionLocal
from app.ai_extraction.router import router as ai_extraction_router
from app.auth import router as auth_router
from app.companies import router as companies_router
from app.decision_engine.router import router as decision_engine_router
from app.document_module.router import router as document_module_router
from app.ingestion import health_router as ingestion_health_router, opendata_router as ingestion_opendata_router, settings_router as ingestion_settings_router
from app.ingestion.eis_browser import router as ingestion_eis_browser_router
from app.ingestion.eis_site import router as ingestion_eis_site_router
from app.ingestion.scheduler import scheduler as ingestion_scheduler
from app.risk.router import router as risk_router
from app.tender_alerts import router as tender_alerts_router
from app.tender_analysis import router as tender_analysis_router
from app.tender_decisions import router as tender_decisions_router
from app.tender_documents import router as tender_documents_router
from app.tender_finance import router as tender_finance_router
from app.tender_tasks import router as tender_tasks_router
from app.tender_tasks.scheduler import scheduler as tender_task_scheduler
from app.tenders import router as tenders_router
from app.telegram_notify import router as telegram_notify_router
from app.telegram_notify.scheduler import scheduler as telegram_notify_scheduler
from app.monitoring.router import router as monitoring_router, settings_router as monitoring_settings_router
from app.monitoring.scheduler import scheduler as monitoring_scheduler
from app.users import router as users_router
from app.web import router as web_router

logger = logging.getLogger(__name__)
app = FastAPI(title="Tender AI Backend Core", version="1.0.0")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")

app.include_router(auth_router)
app.include_router(companies_router)
app.include_router(ingestion_settings_router)
app.include_router(ingestion_opendata_router)
app.include_router(ingestion_eis_site_router)
app.include_router(ingestion_eis_browser_router)
app.include_router(ingestion_health_router)
app.include_router(monitoring_settings_router)
app.include_router(monitoring_router)
app.include_router(tenders_router)
app.include_router(tender_alerts_router)
app.include_router(ai_extraction_router)
app.include_router(document_module_router)
app.include_router(tender_analysis_router)
app.include_router(tender_decisions_router)
app.include_router(tender_finance_router)
app.include_router(decision_engine_router)
app.include_router(tender_documents_router)
app.include_router(tender_tasks_router)
app.include_router(risk_router)
app.include_router(telegram_notify_router)
app.include_router(users_router)
app.include_router(web_router)


def _db_error_reason(db_error: str | None) -> str | None:
    if not db_error:
        return None
    lowered = db_error.lower()
    if "invalidpassworderror" in lowered or "password authentication failed" in lowered:
        return "db_auth_mismatch"
    if "timeout" in lowered:
        return "db_timeout"
    return "db_unavailable"


async def _db_ready() -> tuple[bool, str | None]:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True, None
    except SQLAlchemyError as exc:
        return False, f"{exc.__class__.__name__}: {exc}"
    except Exception as exc:  # pragma: no cover
        return False, f"{exc.__class__.__name__}: {exc}"


@app.get("/health")
async def health(response: Response) -> dict[str, object]:
    db_ok, db_error = await _db_ready()
    db_reason = _db_error_reason(db_error)
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ok": db_ok, "db_ok": db_ok, "db_error": db_error, "db_reason": db_reason}


@app.get("/readiness")
async def readiness(response: Response) -> dict[str, object]:
    db_ok, db_error = await _db_ready()
    db_reason = _db_error_reason(db_error)
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ok": db_ok, "db_ok": db_ok, "db_error": db_error, "db_reason": db_reason}


def _get_migrations_head() -> str:
    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        return script.get_current_head() or "unknown"
    except Exception:
        return "unknown"


@app.get("/version")
async def version() -> dict[str, str]:
    built_at = (
        os.getenv("APP_BUILT_AT_IMAGE")
        or os.getenv("APP_BUILT_AT")
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    version_value = os.getenv("APP_VERSION_IMAGE") or os.getenv("APP_VERSION") or "unknown"
    return {
        "version": version_value,
        "built_at": built_at,
        "migrations_head": _get_migrations_head(),
    }


@app.on_event("startup")
async def startup_event() -> None:
    db_ok, db_error = await _db_ready()
    if not db_ok:
        db_reason = _db_error_reason(db_error)
        logger.error("startup db preflight failed: reason=%s error=%s", db_reason, db_error)
        raise RuntimeError(f"startup db preflight failed: reason={db_reason} error={db_error}")
    logger.info("startup db preflight ok")
    await tender_task_scheduler.start()
    await ingestion_scheduler.start()
    await telegram_notify_scheduler.start()
    await monitoring_scheduler.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await tender_task_scheduler.stop()
    await ingestion_scheduler.stop()
    await telegram_notify_scheduler.stop()
    await monitoring_scheduler.stop()
