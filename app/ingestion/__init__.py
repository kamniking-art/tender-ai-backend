from app.ingestion.eis_site import router as eis_site_router
from app.ingestion.router import health_router, opendata_router, settings_router

__all__ = ["settings_router", "opendata_router", "eis_site_router", "health_router"]
