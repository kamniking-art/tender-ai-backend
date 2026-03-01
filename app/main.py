from fastapi import FastAPI

from app.auth import router as auth_router
from app.companies import router as companies_router
from app.ingestion import health_router as ingestion_health_router, opendata_router as ingestion_opendata_router, settings_router as ingestion_settings_router
from app.ingestion.scheduler import scheduler as ingestion_scheduler
from app.tender_alerts import router as tender_alerts_router
from app.tender_analysis import router as tender_analysis_router
from app.tender_decisions import router as tender_decisions_router
from app.tender_documents import router as tender_documents_router
from app.tender_tasks import router as tender_tasks_router
from app.tender_tasks.scheduler import scheduler as tender_task_scheduler
from app.tenders import router as tenders_router
from app.users import router as users_router

app = FastAPI(title="Tender AI Backend Core", version="1.0.0")

app.include_router(auth_router)
app.include_router(companies_router)
app.include_router(ingestion_settings_router)
app.include_router(ingestion_opendata_router)
app.include_router(ingestion_health_router)
app.include_router(tenders_router)
app.include_router(tender_alerts_router)
app.include_router(tender_analysis_router)
app.include_router(tender_decisions_router)
app.include_router(tender_documents_router)
app.include_router(tender_tasks_router)
app.include_router(users_router)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.on_event("startup")
async def startup_event() -> None:
    await tender_task_scheduler.start()
    await ingestion_scheduler.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await tender_task_scheduler.stop()
    await ingestion_scheduler.stop()
