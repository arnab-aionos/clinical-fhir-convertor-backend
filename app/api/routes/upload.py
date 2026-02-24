"""
Upload Route — POST /api/v1/upload

Accepts a PDF, JPEG, or PNG file. Creates a job record and launches
the full processing pipeline as a background task.

Pipeline stages:
  Stage 1   — PDF / image → raw text  (PyMuPDF direct or Surya OCR)
  Stage 2   — raw text → structured JSON  (LLM extraction + confidence scoring)
  Stage 2.5 — structured JSON → Excel workbook  (cross-verification artifact)
              Status transitions to "awaiting_verification" here.
  Stage 3   — FHIR bundle generation  (triggered explicitly via POST /generate-fhir)
"""

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile, File

from app.core.config import get_settings
from app.db.database import create_job, update_job
from app.models.job_models import JobResponse, JobStatus
from app.services.pdf_processor import process_pdf
from app.services.llm_extractor import extract_clinical_data
from app.services.excel_exporter import ExcelExporter

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
    """
    Full background pipeline: text extraction → LLM extraction → Excel workbook.
    Halts at "awaiting_verification". Stage 3 (FHIR) requires an explicit
    POST /api/v1/jobs/{job_id}/generate-fhir call.
    """
    try:
        await update_job(job_id, status="processing")

        # Stage 1: PDF / image → raw text
        logger.info("[%s] Stage 1: PDF processing…", job_id)
        result = await asyncio.to_thread(process_pdf, file_path)

        await update_job(
            job_id,
            raw_text=result.raw_text,
            ocr_method=result.ocr_method,
            page_count=result.page_count,
        )
        logger.info(
            "[%s] Stage 1 complete: %s, %d page(s).",
            job_id, result.ocr_method, result.page_count,
        )

        # Stage 2: LLM classification + extraction
        logger.info("[%s] Stage 2: LLM extraction…", job_id)
        doc_type, extracted = await asyncio.to_thread(extract_clinical_data, result.raw_text)

        await update_job(job_id, document_type=doc_type, extracted_data=extracted)
        logger.info("[%s] Stage 2 complete. Document type: %s", job_id, doc_type)

        # Stage 2.5: Excel cross-verification workbook
        logger.info("[%s] Stage 2.5: Generating Excel cross-verification workbook…", job_id)
        excel_path: str | None = None
        try:
            exporter = ExcelExporter(upload_dir=settings.upload_dir)
            excel_path = await asyncio.to_thread(
                exporter.generate, job_id, doc_type, extracted
            )
            logger.info("[%s] Excel workbook saved: %s", job_id, excel_path)
        except Exception as excel_exc:
            # Excel failure is non-fatal — log warning and continue
            logger.warning(
                "[%s] Excel generation failed (non-fatal, excel_export_path will be null): %s",
                job_id, excel_exc,
            )

        await update_job(
            job_id,
            status="awaiting_verification",
            excel_export_path=excel_path,
        )
        logger.info(
            "[%s] Pipeline paused at awaiting_verification. "
            "Download Excel at GET /jobs/%s/excel. "
            "Call POST /jobs/%s/generate-fhir to proceed to Stage 3.",
            job_id, job_id, job_id,
        )

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

    Returns a **job_id** immediately (HTTP 202). Poll `GET /api/v1/jobs/{job_id}`
    to track status.

    When `status == "awaiting_verification"`:
      - Download the Excel cross-verification workbook:
          `GET /api/v1/jobs/{job_id}/excel`
      - Optionally correct extracted data:
          `PUT /api/v1/jobs/{job_id}/extracted`
      - Proceed to FHIR generation:
          `POST /api/v1/jobs/{job_id}/generate-fhir`
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

    job_id    = str(uuid.uuid4())
    safe_name = f"{job_id}{suffix}"
    file_path = upload_dir / safe_name

    content = await file.read()
    file_path.write_bytes(content)
    logger.info("Saved upload: %s (%d bytes)", file_path, len(content))

    # Create job record
    job = await create_job(
        job_id=job_id,
        filename=file.filename or safe_name,
        file_path=str(file_path),
    )

    # Launch background pipeline
    background_tasks.add_task(_run_pipeline, job_id, str(file_path))

    from datetime import datetime
    return JobResponse(
        job_id=job["id"],
        status=JobStatus(job["status"]),
        filename=job["filename"],
        document_type=None,
        error_message=job.get("error_message"),
        created_at=datetime.fromisoformat(job["created_at"]),
        updated_at=datetime.fromisoformat(job["updated_at"]),
        excel_export_path=job.get("excel_export_path"),
    )
