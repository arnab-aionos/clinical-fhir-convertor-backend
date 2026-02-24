# clinical-fhir-convertor

Backend API for converting Indian clinical documents (discharge summaries and diagnostic reports) into ABDM/NHCX-compliant FHIR R4 bundles.

Built for **NHCX Hackathon Problem Statement 2** — Clinical Documents to FHIR Structured Data Convertor.

---

## Problem Statement

Indian hospitals produce clinical documents as unstructured PDFs. ABDM (Ayushman Bharat Digital Mission) and NHCX (National Health Claims Exchange) require data in FHIR R4 format. There is no automated pipeline that takes a scanned or digital Indian hospital PDF and produces a structurally valid FHIR R4 bundle with NHCX profile compliance.

This service provides that pipeline.

---

## Features

- Accepts PDF, JPEG, and PNG uploads
- Dual-path text extraction: direct PyMuPDF extraction for digital PDFs; Surya OCR for scanned/image PDFs (threshold: 100 characters)
- Indian medical abbreviation expansion before LLM extraction (100+ abbreviations: C/O, H/O, K/C/O, DM, HTN, LSCS, etc.)
- LLM-based document classification (discharge summary vs. diagnostic report)
- Multi-page chunked extraction with merge for large documents
- Per-field-group confidence scoring (0.0–1.0) with green/amber/red labels
- Stage 2.5 Excel cross-verification workbook (openpyxl) before FHIR generation
- Human review step: extracted data is editable via PUT /extracted before FHIR generation
- FHIR R4 bundle generation using raw dict construction (not fhir.resources library)
- Schema-driven FHIR validation using official HL7 FHIR R4 JSON Schema (fhir.schema.json) via jsonschema Draft6Validator; lightweight required-field fallback when schema is absent
- NHCX compliance warnings for missing meta.profile, missing patient identifiers, and Observations without LOINC codes
- Async SQLite persistence (aiosqlite) — all pipeline artifacts stored per job
- Job DELETE endpoint for cleanup
- Swagger UI at /docs, ReDoc at /redoc

---

## Tech Stack

| Component | Library / Version |
|-----------|-------------------|
| API framework | FastAPI 0.115.6 |
| ASGI server | uvicorn[standard] 0.34.0 |
| Database | aiosqlite 0.20.0 (SQLite) |
| PDF text extraction | PyMuPDF 1.25.3 |
| OCR (scanned PDFs) | surya-ocr 0.7.0 |
| Image processing | Pillow 10.4.0 |
| LLM API | groq 0.15.0 (Llama 3.3 70B via Groq cloud) |
| FHIR schema validation | jsonschema (Draft6Validator) + fhir.schema.json |
| Excel export | openpyxl 3.1.5 |
| Data validation | pydantic 2.10.6, pydantic-settings 2.7.1 |
| Date parsing | python-dateutil 2.9.0 |

---

## Architecture

```
Client
  │
  ▼
POST /api/v1/upload ──► FastAPI BackgroundTask
                            │
                   ─────────────────────────────────────────
                   Stage 1:  PyMuPDF (direct text)
                             OR Surya OCR (scanned PDF/image)
                             ↓
                   Stage 2:  Abbreviation Expander
                             ↓ Groq classify
                             ↓ Groq extract (chunked if >10k chars)
                             ↓ Confidence scoring
                             ↓
                   Stage 2.5: Excel workbook (openpyxl)
                             ↓
                   status → awaiting_verification
                   ─────────────────────────────────────────
                             │
                             ▼ (user calls POST /generate-fhir)
                   Stage 3:  FHIR Mapper (dict builder)
                             ↓
                             FHIR Validator (jsonschema + NHCX warnings)
                             ↓
                   status → completed

SQLite database (aiosqlite)
  └── jobs table: id, status, filename, file_path, document_type,
                  raw_text, ocr_method, page_count,
                  extracted_data (JSON), fhir_bundle (JSON),
                  validation_report (JSON), excel_export_path,
                  error_message, created_at, updated_at
```

---

## FHIR R4 Compliance

All generated bundles use raw Python dicts serialised to JSON. The `fhir.resources` library is installed but intentionally not used for bundle construction — version 7.1.0 implements FHIR R5, which has breaking structural differences from R4 (e.g., `Composition.subject` changed from single reference to array, `Encounter.period` renamed to `actualPeriod`, `Procedure.performedDateTime` renamed to `occurrenceDateTime`).

**Discharge Summary bundle** (`type: document`):
Resources: Composition, Patient, Organization, Practitioner (optional), Encounter, Condition (per diagnosis), Procedure (per procedure), Observation (per vital with LOINC code), Observation (per investigation), MedicationStatement (per medication)

**Diagnostic Report bundle** (`type: collection`):
Resources: DiagnosticReport, Patient, Organization (laboratory), Practitioner (referring doctor, optional), Observation (per test parameter)

NHCX profile URLs follow the ABDM IG:
`https://nrces.in/ndhm/fhir/r4/StructureDefinition/NHCX{ResourceType}`

LOINC codes applied to vital Observations: blood pressure (55284-4), pulse rate (8867-4), body temperature (8310-5), oxygen saturation (59408-5), respiratory rate (9279-1), body weight (29463-7), body height (8302-2).

---

## Supported Document Types

| Document Type | Bundle Type | Key Resources |
|---------------|-------------|---------------|
| Discharge Summary | document | Composition, Patient, Encounter, Condition, Procedure, Observation, MedicationStatement |
| Diagnostic Report | collection | DiagnosticReport, Patient, Observation |
| Unknown | collection | Falls back to diagnostic report mapping |

Documents are auto-classified by the LLM using the first 3,000 characters of expanded text.

---

## Prerequisites

- Python 3.11 or 3.12
- A Groq API key (free tier: 14,400 requests/day, no credit card required) — https://console.groq.com
- `fhir.schema.json` placed at the backend root directory (download from hl7.org or https://build.fhir.org/fhir.schema.json)
- On first scanned PDF upload, Surya OCR downloads ~500MB of model weights from HuggingFace. Requires internet access.
```

---

## Installation

```bash
git clone <repo-url>
cd clinical-fhir-convertor

python -m venv venv
# Windows
venv\Scripts\activate
# Linux/macOS
source venv/bin/activate

pip install -r requirements.txt
```

Apply the Surya OCR patches described in the Prerequisites section.

Copy `.env.example` to `.env` and fill in:
```
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.3-70b-versatile
UPLOAD_DIR=uploads
DATABASE_URL=./clinical_fhir.db
APP_ENV=development
LOG_LEVEL=INFO
PDF_TEXT_THRESHOLD=100
```

Place `fhir.schema.json` in the `clinical-fhir-convertor/` root directory.

---

## Running Locally

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Swagger UI: http://localhost:8000/docs
ReDoc: http://localhost:8000/redoc
Health check: http://localhost:8000/health

The SQLite database (`clinical_fhir.db`) and upload files are created automatically on first startup.

---

## API Reference

All endpoints are prefixed `/api/v1`.

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info (name, version, status) |
| GET | `/health` | Returns `{"status": "ok"}` |

### Upload

| Method | Path | Status | Description |
|--------|------|--------|-------------|
| POST | `/api/v1/upload` | 202 | Upload a clinical PDF/image. Returns `job_id` immediately. |

Request: `multipart/form-data`, field name `file`. Accepted types: `.pdf`, `.jpg`, `.jpeg`, `.png`.

### Jobs

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/jobs` | List 50 most recent jobs (newest first) |
| GET | `/api/v1/jobs/{job_id}` | Job status and metadata |
| GET | `/api/v1/jobs/{job_id}/text` | Raw OCR/extracted text, OCR method, page count |
| GET | `/api/v1/jobs/{job_id}/extracted` | Extracted structured data + confidence scores |
| PUT | `/api/v1/jobs/{job_id}/extracted` | Update extracted data (human review) |
| POST | `/api/v1/jobs/{job_id}/generate-fhir` | Generate FHIR bundle; status → completed |
| GET | `/api/v1/jobs/{job_id}/fhir` | Retrieve stored FHIR R4 bundle |
| GET | `/api/v1/jobs/{job_id}/validation` | Retrieve validation report |
| GET | `/api/v1/jobs/{job_id}/excel` | Download Stage 2.5 Excel workbook (.xlsx) |
| DELETE | `/api/v1/jobs/{job_id}` | Delete job record (file on disk is NOT deleted) |

**Job status flow:**
`pending → processing → awaiting_verification → completed`
`failed` is a terminal state reachable from any stage.

**PUT /extracted** is allowed when status is `awaiting_verification`, `completed`, or `failed`.
**POST /generate-fhir** is allowed when status is `awaiting_verification` or `completed`.

---

## Project Structure

```
clinical-fhir-convertor/
├── main.py                          # FastAPI app, CORS, lifespan, health endpoints
├── requirements.txt
├── fhir.schema.json                 # HL7 FHIR R4 JSON Schema (place here manually)
├── .env                             # Secrets and config (not committed)
├── uploads/                         # Uploaded files and generated Excel workbooks
├── clinical_fhir.db                 # SQLite database (created on startup)
└── app/
    ├── core/
    │   └── config.py                # Pydantic Settings (reads .env)
    ├── db/
    │   └── database.py              # aiosqlite CRUD, migration guard
    ├── models/
    │   ├── job_models.py            # JobStatus enum, all JobResponse Pydantic models
    │   └── extracted_data.py        # DischargeSummaryData, DiagnosticReportData models
    ├── api/
    │   └── routes/
    │       ├── upload.py            # POST /upload, background pipeline
    │       └── jobs.py              # All job-management endpoints
    └── services/
        ├── pdf_processor.py         # PyMuPDF + Surya OCR, PDFProcessingResult
        ├── abbreviation_expander.py # Indian clinical abbreviation table + regex expand()
        ├── llm_extractor.py         # Groq classify + extract + chunking + merge
        ├── confidence_scorer.py     # LLM-based and heuristic confidence scoring
        ├── excel_exporter.py        # openpyxl workbook builder (ExcelExporter)
        ├── fhir_mapper.py           # FHIR R4 bundle dict builders
        └── fhir_validator.py        # jsonschema Draft6Validator + NHCX warnings
```

---

## Deployment

### systemd Service Unit

Create `/etc/systemd/system/clinical-fhir-convertor.service`:

```ini
[Unit]
Description=Clinical FHIR Convertor API
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/clinical-fhir-convertor
EnvironmentFile=/opt/clinical-fhir-convertor/.env
ExecStart=/opt/clinical-fhir-convertor/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable clinical-fhir-convertor
sudo systemctl start clinical-fhir-convertor
sudo journalctl -u clinical-fhir-convertor -f
```

**Note:** Use `--workers 1`. Surya OCR model singletons are not safe to share across processes; multi-worker setups require a separate model-serving process.

---

## Troubleshooting

**`KeyError: 'encoder'` on first scanned PDF upload**
Surya OCR bug #14. The recognition model config constructor requires the `encoder` and `decoder` kwargs to be extracted before calling `super().__init__()`. Apply the patch described in Prerequisites.

**`ValueError: Multiple text sub-configs found`**
Surya OCR bug #16. Occurs with `transformers >= 4.57`. Add the `get_text_config` override to `SuryaOCRConfig`. See Prerequisites.

**`AssertionError: Pillow version X is not supported`**
The `surya-ocr==0.7.0` package requires `Pillow==10.4.0`. Do not upgrade Pillow. If a later version was installed, run: `pip install Pillow==10.4.0`

**Groq rate limit errors (HTTP 429)**
The free Groq tier allows 14,400 requests per day and 30 requests per minute. Each document consumes approximately 3–5 API calls. The extractor backs off automatically on 429 responses. For batch testing, space uploads at least 10 seconds apart.

**FHIR validation falls back to lightweight mode**
If `fhir.schema.json` is not found at the backend root, validation uses the lightweight required-field check instead of the full JSON Schema validator. The startup log will show: `fhir.schema.json not found — falling back to lightweight validation`. Place the file at `clinical-fhir-convertor/fhir.schema.json`.

**Scanned PDF processing is slow (~3–5 minutes)**
This is expected on a CPU-only machine. Surya OCR loads detection and recognition models (~500MB combined) on the first call. Subsequent calls within the same process reuse the cached models and are faster (~30–60 seconds per page).

---

## Known Limitations

- **Single-worker only.** Surya OCR model singletons cannot be safely shared across multiple uvicorn worker processes. Horizontal scaling requires a separate model-serving layer.
- **SQLite not suitable for concurrent write load.** Fine for demo and single-user use. Replace with PostgreSQL + asyncpg for production multi-user workloads.
- **Groq TPD limit.** The free tier allows 14,400 requests per day. Each document uses 3–5 requests (classify + extract + confidence). Approximately 3,000–4,000 documents can be processed per day before the limit is hit.
- **Uploaded files are not deleted when a job is deleted.** The DELETE /jobs/{job_id} endpoint removes the database record but leaves the file in the `uploads/` directory.
- **FHIR R4 validation uses dict-level schema checks.** The validator does not use FHIR's official reference implementation (HAPI Validator) and cannot catch semantic or terminology errors.
- **LLM extraction accuracy depends on OCR quality.** For low-resolution scans, Surya OCR may produce garbled text, leading to partial or incorrect extractions. The confidence scorer will rate affected fields low (amber or red).
- **Multi-page chunking may lose context across page boundaries.** Section headers on one page and values on the next may result in null extractions. This is mitigated by the first-non-null merge strategy.

---

## Hackathon Context

**Event:** NHCX Hackathon 2026 — National Health Claims Exchange
**Problem Statement:** PS2 — Clinical Documents to FHIR Structured Data Convertor
**Participant:** Arnab Das, Software Engineer, Aionos
**Demo Date:** 3 March 2026

**PS2 objective:** Automatically convert Indian hospital clinical PDFs (discharge summaries and diagnostic reports) into ABDM/NHCX-compliant FHIR R4 bundles. The pipeline must handle both digital and scanned PDFs, support both document types, allow human review before FHIR generation, and produce bundles with NHCX profile annotations.

**Technical constraints at build time:**
- CPU-only Windows 11 machine, 16GB RAM
- No GPU available for OCR or LLM inference
- All tooling must be free and open-source (or free-tier cloud APIs)
- Groq API provides Llama 3.3 70B inference at zero cost within the free tier
