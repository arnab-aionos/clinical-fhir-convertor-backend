"""
Upload Route
────────────
POST /api/v1/upload

Accepts a PDF, JPEG, or PNG file. Creates a job record and launches
the full processing pipeline (OCR → LLM extraction) as a background task.
"""

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile, File

from app.core.config import get_settings
from app.db.database import create_job, update_job
from app.models.job_models import JobResponse
from app.services.pdf_processor import process_pdf
from app.services.llm_extractor import extract_clinical_data
from app.services.fhir_mapper import generate_fhir_bundle
from app.services.fhir_validator import validate_fhir_bundle

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

_ALLOWED_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/jpg",
}
_ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


async def _run_pipeline(job_id: str, file_path: str) -> None:
    """Full background pipeline: PDF processing → OCR → LLM extraction → FHIR generation."""
    try:
        await update_job(job_id, status="processing")

        # Step 1: PDF / image → raw text (CPU-bound, run in thread pool)
        logger.info("[%s] Starting PDF processing…", job_id)
        result = await asyncio.to_thread(process_pdf, file_path)

        await update_job(
            job_id,
            raw_text=result.raw_text,
            ocr_method=result.ocr_method,
            page_count=result.page_count,
        )
        logger.info("[%s] PDF processed via %s (%d pages).", job_id, result.ocr_method, result.page_count)

        # Step 2: LLM classification + extraction (I/O-bound via Groq API)
        logger.info("[%s] Starting LLM extraction…", job_id)
        doc_type, extracted = await asyncio.to_thread(extract_clinical_data, result.raw_text)

        await update_job(
            job_id,
            document_type=doc_type,
            extracted_data=extracted,
            status="completed",
        )
        logger.info("[%s] LLM extraction complete. Document type: %s", job_id, doc_type)

        # Step 3: Auto-generate FHIR bundle + validation report
        logger.info("[%s] Auto-generating FHIR bundle…", job_id)
        bundle = await asyncio.to_thread(generate_fhir_bundle, doc_type, extracted)
        validation = await asyncio.to_thread(validate_fhir_bundle, bundle)
        await update_job(job_id, fhir_bundle=bundle, validation_report=validation.to_dict())
        logger.info("[%s] FHIR bundle ready. %d resources, valid=%s",
                    job_id, validation.resource_count, validation.is_valid)

    except Exception as exc:
        logger.exception("[%s] Pipeline failed: %s", job_id, exc)
        await update_job(job_id, status="failed", error_message=str(exc))


@router.post("/upload", response_model=JobResponse, status_code=202)
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """
    Upload a clinical PDF (discharge summary or diagnostic report).

    Returns a **job_id** immediately. Poll `GET /api/v1/jobs/{job_id}` to
    track processing status. When status is `completed`, fetch extracted data
    and generate a FHIR bundle.
    """
    # Validate file type
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{suffix}'. Allowed: {_ALLOWED_EXTENSIONS}",
        )

    # Save to upload directory
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    job_id = str(uuid.uuid4())
    safe_name = f"{job_id}{suffix}"
    file_path = upload_dir / safe_name

    content = await file.read()
    file_path.write_bytes(content)
    logger.info("Saved upload: %s (%d bytes)", file_path, len(content))

    # Create job record
    job = await create_job(job_id=job_id, filename=file.filename or safe_name, file_path=str(file_path))

    # Launch background pipeline
    background_tasks.add_task(_run_pipeline, job_id, str(file_path))

    from app.models.job_models import JobStatus, DocumentType
    from datetime import datetime
    return JobResponse(
        job_id=job["id"],
        status=JobStatus(job["status"]),
        filename=job["filename"],
        document_type=None,
        error_message=job.get("error_message"),
        created_at=datetime.fromisoformat(job["created_at"]),
        updated_at=datetime.fromisoformat(job["updated_at"]),
    )
