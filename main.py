"""
NHCX Hackathon PS2 – Clinical Documents to FHIR Convertor

FastAPI entry point.

Run locally:
  uvicorn main:app --reload --host 0.0.0.0 --port 8000

Interactive docs:
  http://localhost:8000/docs
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.db.database import init_db
from app.api.routes.upload import router as upload_router
from app.api.routes.jobs import router as jobs_router

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Clinical FHIR Convertor…")
    await init_db()
    logger.info("SQLite database initialised at %s", settings.database_url)
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="UniHealth — Clinical to FHIR Convertor",
    description=(
        "NHCX Hackathon PS2 – Converts Indian clinical PDFs (discharge summaries, "
        "diagnostic reports) into ABDM/NHCX-compliant FHIR R4 bundles using "
        "Surya OCR + Groq Llama 3.3 70B."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Allow all origins during development (tighten for production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(upload_router, prefix="/api/v1", tags=["Upload"])
app.include_router(jobs_router, prefix="/api/v1/jobs", tags=["Jobs"])


@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "UniHealth — Clinical to FHIR Convertor",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}
