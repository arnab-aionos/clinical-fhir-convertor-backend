"""
Jobs Route

GET  /api/v1/jobs                           – List recent jobs (last 50, newest first)
GET  /api/v1/jobs/{job_id}                  – Job status + metadata
GET  /api/v1/jobs/{job_id}/text             – Raw OCR/extracted text
GET  /api/v1/jobs/{job_id}/extracted        – LLM-extracted structured JSON
PUT  /api/v1/jobs/{job_id}/extracted        – Human review: update extracted data
POST /api/v1/jobs/{job_id}/generate-fhir    – Generate FHIR bundle from extracted data
GET  /api/v1/jobs/{job_id}/fhir             – Fetch generated FHIR bundle
GET  /api/v1/jobs/{job_id}/validation       – Fetch FHIR validation report
GET  /api/v1/jobs/{job_id}/excel            – Download Stage 2.5 Excel cross-verification workbook
GET  /api/v1/jobs/{job_id}/stream          – SSE stream of job status updates (replaces client polling)
"""

import asyncio
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app.db.database import delete_job, get_all_jobs, get_job, update_job
from app.models.job_models import (
    ConfidenceDetail,
    DocumentType,
    JobExtractedResponse,
    JobFhirResponse,
    JobResponse,
    JobStatus,
    JobTextResponse,
    JobValidationResponse,
    PaginatedJobsResponse,
)
from app.services.fhir_mapper import generate_fhir_bundle
from app.services.fhir_validator import validate_fhir_bundle

logger = logging.getLogger(__name__)
router = APIRouter()

# Statuses that allow FHIR generation:
#   "awaiting_verification" — normal post-Stage-2.5 gate
#   "completed"             — re-generation after human review edits
_FHIR_ALLOWED_STATUSES = {"awaiting_verification", "completed"}

# Statuses that allow human updates to extracted data
_UPDATE_ALLOWED_STATUSES = {"awaiting_verification", "completed", "failed"}


# Helpers

async def _require_job(job_id: str) -> dict:
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job


def _to_response(job: dict) -> JobResponse:
    doc_type = job.get("document_type")
    return JobResponse(
        job_id=job["id"],
        status=JobStatus(job["status"]),
        filename=job["filename"],
        document_type=DocumentType(doc_type) if doc_type in ("discharge_summary", "diagnostic_report") else None,
        error_message=job.get("error_message"),
        created_at=datetime.fromisoformat(job["created_at"]),
        updated_at=datetime.fromisoformat(job["updated_at"]),
        excel_export_path=job.get("excel_export_path"),
    )


# Terminal statuses — SSE stream closes when any of these is reached
_TERMINAL_STATUSES = {"completed", "awaiting_verification", "failed"}

# IMPORTANT: this static route must be registered BEFORE /{job_id} to avoid
# FastAPI treating "jobs" as a job_id path parameter.

@router.get("/{job_id}/stream")
async def stream_job_status(job_id: str, request: Request):
    """Stream job status as Server-Sent Events.

    Polls the DB every 0.5 s and emits a ``data:`` line whenever the status
    changes. Closes the stream on terminal status (completed / awaiting_verification
    / failed). A comment heartbeat is sent every ~15 s to prevent proxy timeouts.

    The frontend opens this with ``new EventSource('/api/v1/jobs/{id}/stream')``
    instead of polling ``GET /api/v1/jobs/{id}`` every few seconds.
    """
    async def _events():
        last_status: str | None = None
        ticks = 0

        while True:
            if await request.is_disconnected():
                break

            job = await get_job(job_id)
            if job is None:
                yield 'event: error\ndata: {"error": "job not found"}\n\n'
                break

            resp = _to_response(job)

            if resp.status != last_status:
                yield f"data: {resp.model_dump_json()}\n\n"
                last_status = resp.status

            if resp.status in _TERMINAL_STATUSES:
                break

            ticks += 1
            if ticks % 30 == 0:      # heartbeat every ~15 s (30 × 0.5 s)
                yield ": heartbeat\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # prevent nginx / caddy buffering
        },
    )


@router.get("", response_model=PaginatedJobsResponse)
async def list_jobs(
    document_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
):
    """Return paginated jobs, newest first. Optionally filtered by document_type."""
    valid_type = document_type if document_type in ("discharge_summary", "diagnostic_report") else None
    jobs_data, total = await get_all_jobs(
        document_type=valid_type,
        page=page,
        page_size=page_size,
    )
    total_pages = max(1, math.ceil(total / page_size))
    return PaginatedJobsResponse(
        jobs=[_to_response(j) for j in jobs_data],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )



@router.get("/{job_id}", response_model=JobResponse)
async def get_job_status(job_id: str):
    """Get current processing status and metadata for a job."""
    job = await _require_job(job_id)
    return _to_response(job)



@router.get("/{job_id}/text", response_model=JobTextResponse)
async def get_job_text(job_id: str):
    """Return the raw text extracted from the document (after OCR or direct extraction)."""
    job = await _require_job(job_id)
    if job["status"] in ("pending", "processing"):
        raise HTTPException(status_code=202, detail="Job is still processing. Try again shortly.")
    return JobTextResponse(
        job_id=job["id"],
        status=JobStatus(job["status"]),
        raw_text=job.get("raw_text"),
        ocr_method=job.get("ocr_method"),
        page_count=job.get("page_count"),
    )



@router.get("/{job_id}/extracted", response_model=JobExtractedResponse)
async def get_extracted_data(job_id: str):
    """Return LLM-extracted structured clinical data with per-field confidence scores."""
    job = await _require_job(job_id)
    if job["status"] in ("pending", "processing"):
        raise HTTPException(status_code=202, detail="Job is still processing. Try again shortly.")
    if job["status"] == "failed":
        raise HTTPException(status_code=500, detail=job.get("error_message", "Processing failed"))
    doc_type = job.get("document_type")
    extracted = job.get("extracted_data") or {}
    # Surface _confidence as a top-level convenience field for the frontend
    raw_confidence = extracted.get("_confidence") or {}
    confidence: dict[str, ConfidenceDetail] | None = None
    if raw_confidence:
        confidence = {}
        for k, v in raw_confidence.items():
            if isinstance(v, dict) and "score" in v and "label" in v and "color" in v:
                confidence[k] = ConfidenceDetail(**v)
            # Skip malformed entries rather than creating invalid objects
    return JobExtractedResponse(
        job_id=job["id"],
        status=JobStatus(job["status"]),
        document_type=DocumentType(doc_type) if doc_type in ("discharge_summary", "diagnostic_report") else None,
        extracted_data=extracted,
        confidence=confidence,
    )



class UpdateExtractedRequest(BaseModel):
    extracted_data: dict[str, Any]


@router.put("/{job_id}/extracted", response_model=JobExtractedResponse)
async def update_extracted_data(job_id: str, body: UpdateExtractedRequest):
    """
    Human review endpoint — update the extracted data before or after FHIR generation.
    Useful for correcting OCR errors or LLM extraction mistakes.

    Status is NOT changed by this call. Call POST /generate-fhir when ready.
    Allowed statuses: awaiting_verification, completed, failed.
    """
    job = await _require_job(job_id)
    if job["status"] not in _UPDATE_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot update extracted data when job status is '{job['status']}'. "
                f"Allowed statuses: {sorted(_UPDATE_ALLOWED_STATUSES)}"
            ),
        )
    await update_job(job_id, extracted_data=body.extracted_data)
    updated_job = await _require_job(job_id)
    doc_type = updated_job.get("document_type")
    return JobExtractedResponse(
        job_id=updated_job["id"],
        status=JobStatus(updated_job["status"]),
        document_type=DocumentType(doc_type) if doc_type in ("discharge_summary", "diagnostic_report") else None,
        extracted_data=updated_job.get("extracted_data"),
    )



@router.post("/{job_id}/generate-fhir", response_model=JobFhirResponse)
async def generate_fhir(job_id: str):
    """
    Generate (or re-generate) the FHIR R4 bundle from the current extracted data.
    Also runs structural validation and stores the validation report.

    Allowed when status is:
      - "awaiting_verification" — normal flow after Stage 2.5 cross-verification
      - "completed"             — re-generation after human review edits

    On success, job status transitions to "completed".
    """
    job = await _require_job(job_id)
    if job["status"] not in _FHIR_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot generate FHIR bundle when job status is '{job['status']}'. "
                f"Allowed statuses: {sorted(_FHIR_ALLOWED_STATUSES)}"
            ),
        )
    extracted = job.get("extracted_data")
    if not extracted:
        raise HTTPException(status_code=400, detail="No extracted data found for this job")
    doc_type = job.get("document_type") or "unknown"

    # FHIR generation (CPU-bound)
    bundle = await asyncio.to_thread(generate_fhir_bundle, doc_type, extracted)

    # Validation
    validation = await asyncio.to_thread(validate_fhir_bundle, bundle)
    validation_dict = validation.to_dict()

    await update_job(
        job_id,
        fhir_bundle=bundle,
        validation_report=validation_dict,
        status="completed",
    )
    logger.info(
        "[%s] FHIR bundle generated. %d resources, valid=%s",
        job_id, validation.resource_count, validation.is_valid,
    )

    return JobFhirResponse(job_id=job_id, fhir_bundle=bundle)



@router.get("/{job_id}/fhir", response_model=JobFhirResponse)
async def get_fhir_bundle(job_id: str):
    """Return the previously generated FHIR bundle."""
    job = await _require_job(job_id)
    fhir = job.get("fhir_bundle")
    if not fhir:
        raise HTTPException(
            status_code=404,
            detail="No FHIR bundle found. Call POST /generate-fhir first."
        )
    return JobFhirResponse(job_id=job_id, fhir_bundle=fhir)



@router.get("/{job_id}/validation", response_model=JobValidationResponse)
async def get_validation_report(job_id: str):
    """Return the FHIR validation report for this job."""
    job = await _require_job(job_id)
    report = job.get("validation_report")
    if not report:
        raise HTTPException(
            status_code=404,
            detail="No validation report found. Call POST /generate-fhir first."
        )
    return JobValidationResponse(
        job_id=job_id,
        is_valid=report.get("is_valid", False),
        errors=report.get("errors", []),
        warnings=report.get("warnings", []),
        resource_count=report.get("resource_count", 0),
    )



@router.get("/{job_id}/excel")
async def get_excel_export(job_id: str):
    """
    Download the Stage 2.5 Excel cross-verification workbook for this job.

    The workbook is generated automatically after LLM extraction completes.
    It becomes available once status is "awaiting_verification" (or later).

    Returns the .xlsx file as a direct file download.
    MIME type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
    """
    job = await _require_job(job_id)
    excel_path_str = job.get("excel_export_path")

    if not excel_path_str:
        raise HTTPException(
            status_code=404,
            detail=(
                "Excel workbook not yet generated for this job. "
                "The pipeline must reach 'awaiting_verification' status first. "
                f"Current status: '{job['status']}'"
            ),
        )

    excel_path = Path(excel_path_str)
    if not excel_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Excel file not found on disk. It may have been moved or deleted.",
        )

    return FileResponse(
        path=str(excel_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=excel_path.name,
    )



@router.delete("/{job_id}", status_code=204)
async def delete_job_record(job_id: str):
    """
    Permanently delete a job and all its associated data from the database.
    The uploaded PDF file on disk is NOT deleted (it remains in the uploads/ folder).
    Returns 204 No Content on success, 404 if the job does not exist.
    """
    deleted = await delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
