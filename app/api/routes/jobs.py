"""
Jobs Route
──────────
GET  /api/v1/jobs/{job_id}                  – Job status + metadata
GET  /api/v1/jobs/{job_id}/text             – Raw OCR/extracted text
GET  /api/v1/jobs/{job_id}/extracted        – LLM-extracted structured JSON
PUT  /api/v1/jobs/{job_id}/extracted        – Human review: update extracted data
POST /api/v1/jobs/{job_id}/generate-fhir    – Generate FHIR bundle from extracted data
GET  /api/v1/jobs/{job_id}/fhir             – Fetch generated FHIR bundle
GET  /api/v1/jobs/{job_id}/validation       – Fetch FHIR validation report
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.database import get_job, update_job
from app.models.job_models import (
    ConfidenceDetail,
    JobExtractedResponse,
    JobFhirResponse,
    JobResponse,
    JobStatus,
    JobTextResponse,
    JobValidationResponse,
    DocumentType,
)
from app.services.fhir_mapper import generate_fhir_bundle
from app.services.fhir_validator import validate_fhir_bundle

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Helpers ──────────────────────────────────────────────────────────────────

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
    )


# ─── GET /jobs/{job_id} ───────────────────────────────────────────────────────

@router.get("/{job_id}", response_model=JobResponse)
async def get_job_status(job_id: str):
    """Get current processing status and metadata for a job."""
    job = await _require_job(job_id)
    return _to_response(job)


# ─── GET /jobs/{job_id}/text ──────────────────────────────────────────────────

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


# ─── GET /jobs/{job_id}/extracted ─────────────────────────────────────────────

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


# ─── PUT /jobs/{job_id}/extracted ─────────────────────────────────────────────

class UpdateExtractedRequest(BaseModel):
    extracted_data: dict[str, Any]


@router.put("/{job_id}/extracted", response_model=JobExtractedResponse)
async def update_extracted_data(job_id: str, body: UpdateExtractedRequest):
    """
    Human review endpoint – update the extracted data before FHIR generation.
    Useful for correcting OCR errors or LLM extraction mistakes.
    """
    job = await _require_job(job_id)
    if job["status"] not in ("completed", "failed"):
        raise HTTPException(status_code=400, detail="Can only update extracted data for completed/failed jobs")
    await update_job(job_id, extracted_data=body.extracted_data, status="completed")
    updated_job = await _require_job(job_id)
    doc_type = updated_job.get("document_type")
    return JobExtractedResponse(
        job_id=updated_job["id"],
        status=JobStatus(updated_job["status"]),
        document_type=DocumentType(doc_type) if doc_type in ("discharge_summary", "diagnostic_report") else None,
        extracted_data=updated_job.get("extracted_data"),
    )


# ─── POST /jobs/{job_id}/generate-fhir ───────────────────────────────────────

@router.post("/{job_id}/generate-fhir", response_model=JobFhirResponse)
async def generate_fhir(job_id: str):
    """
    Generate and store the FHIR R4 bundle from the extracted clinical data.
    Also runs structural validation and stores the validation report.
    """
    job = await _require_job(job_id)
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="Job must be in 'completed' state to generate FHIR bundle")
    extracted = job.get("extracted_data")
    if not extracted:
        raise HTTPException(status_code=400, detail="No extracted data found for this job")
    doc_type = job.get("document_type") or "unknown"

    # FHIR generation (CPU-bound)
    bundle = await asyncio.to_thread(generate_fhir_bundle, doc_type, extracted)

    # Validation
    validation = await asyncio.to_thread(validate_fhir_bundle, bundle)
    validation_dict = validation.to_dict()

    await update_job(job_id, fhir_bundle=bundle, validation_report=validation_dict)

    return JobFhirResponse(job_id=job_id, fhir_bundle=bundle)


# ─── GET /jobs/{job_id}/fhir ──────────────────────────────────────────────────

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


# ─── GET /jobs/{job_id}/validation ───────────────────────────────────────────

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
