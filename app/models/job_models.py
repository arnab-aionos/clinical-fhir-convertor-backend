from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel
from datetime import datetime


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    AWAITING_VERIFICATION = "awaiting_verification"   # Stage 2.5: Excel generated, awaiting explicit FHIR trigger
    COMPLETED = "completed"
    FAILED = "failed"


class DocumentType(str, Enum):
    DISCHARGE_SUMMARY = "discharge_summary"
    DIAGNOSTIC_REPORT = "diagnostic_report"
    UNKNOWN = "unknown"


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    filename: str
    document_type: Optional[DocumentType] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    excel_export_path: Optional[str] = None   # Set after Stage 2.5 Excel generation


class JobTextResponse(BaseModel):
    job_id: str
    status: JobStatus
    raw_text: Optional[str] = None
    ocr_method: Optional[str] = None  # "direct" | "surya_ocr"
    page_count: Optional[int] = None


class ConfidenceDetail(BaseModel):
    score: float          # 0.0 – 1.0
    label: str            # "high" | "medium" | "low"
    color: str            # "green" | "amber" | "red"


class JobExtractedResponse(BaseModel):
    job_id: str
    status: JobStatus
    document_type: Optional[DocumentType] = None
    extracted_data: Optional[dict[str, Any]] = None
    # _confidence is embedded inside extracted_data under key "_confidence".
    # Surfaced here as a convenience top-level field for the frontend.
    confidence: Optional[dict[str, ConfidenceDetail]] = None


class JobFhirResponse(BaseModel):
    job_id: str
    fhir_bundle: Optional[dict[str, Any]] = None


class JobValidationResponse(BaseModel):
    job_id: str
    is_valid: bool
    errors: list[str]
    warnings: list[str]
    resource_count: int


class PaginatedJobsResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
