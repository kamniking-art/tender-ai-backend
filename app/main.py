from fastapi import FastAPI

from app.auth import router as auth_router
from app.companies import router as companies_router
from app.tenders import router as tenders_router
from app.users import router as users_router

app = FastAPI(title="Tender AI Backend Core", version="1.0.0")

app.include_router(auth_router)
app.include_router(companies_router)
app.include_router(tenders_router)
app.include_router(users_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
