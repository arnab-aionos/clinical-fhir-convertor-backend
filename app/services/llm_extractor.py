"""
LLM Extractor Service

Extraction pipeline per document:
  abbreviation expansion → classification → structured extraction → confidence scoring

Multi-page chunking: text > MAX_SINGLE_CALL_CHARS is split at page breaks into
MAX_CHUNK_CHARS slices and extracted independently, then merged:
  - Single-value fields (patient, encounter, vitals): first non-null wins
  - List fields (diagnoses, medications, observations): concatenated + deduplicated
"""

import json
import logging
import time
from typing import Any

from groq import Groq

from app.core.config import get_settings
from app.models.extracted_data import DischargeSummaryData, DiagnosticReportData
from app.services.abbreviation_expander import expand as expand_abbreviations
from app.services.confidence_scorer import score_confidence, annotate_with_confidence

logger = logging.getLogger(__name__)
settings = get_settings()

# Groq client (initialized once per process)
_client: Groq | None = None

def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=settings.groq_api_key)
    return _client


# Character budgets
MAX_SINGLE_CALL_CHARS = 10_000   # Below this, one call is fine
MAX_CHUNK_CHARS       = 8_000    # Max chars per chunk in multi-page mode
CLASSIFY_SNIPPET_CHARS = 3_000   # Classification only needs first N chars
MAX_RETRIES           = 2        # Retry on JSON parse failure


# Prompt templates

_CLASSIFY_SYSTEM = """You are a clinical document classifier for Indian hospital documents.
Classify the document as exactly one of:
  discharge_summary
  diagnostic_report
  unknown
Reply with ONLY that one word. No punctuation, no explanation."""


_DS_SYSTEM = """You are a clinical NLP expert specialising in Indian hospital documents.
The text you receive is OCR output from a scanned clinical discharge summary.
Medical abbreviations have been pre-expanded.

Your task: extract ALL available information and return a single valid JSON object.
Rules:
  - Use null for any field not found in the document.
  - Do NOT invent or hallucinate values.
  - Dates: prefer ISO format YYYY-MM-DD where determinable.
  - Diagnoses: list both provisional and final, set "type" accordingly.
  - Medications: capture drug name, dosage, frequency, duration, route.
  - Investigations: capture every lab result row (parameter, result, unit, ref_range).
  - Vitals: capture each vital sign value as a string including its unit."""


_DS_SCHEMA = """{
  "document_type": "discharge_summary",
  "patient": {
    "name": null,
    "age": null,
    "gender": null,
    "id": null,
    "address": null,
    "dob": null
  },
  "encounter": {
    "admission_date": null,
    "discharge_date": null,
    "department": null,
    "ward": null,
    "bed_number": null,
    "hospital_name": null,
    "hospital_address": null
  },
  "treating_doctor": null,
  "diagnoses": [
    {"text": null, "type": "provisional|final", "icd_code": null}
  ],
  "procedures": [
    {"text": null, "date": null}
  ],
  "chief_complaints": [],
  "history_of_present_illness": null,
  "past_history": null,
  "vitals": {
    "bp": null,
    "pulse": null,
    "temp": null,
    "spo2": null,
    "rr": null,
    "weight": null,
    "height": null
  },
  "investigations": [
    {"test": null, "result": null, "unit": null, "ref_range": null, "is_abnormal": false}
  ],
  "medications": [
    {"drug": null, "dosage": null, "frequency": null, "duration": null, "route": null}
  ],
  "condition_at_discharge": null,
  "follow_up": null,
  "course_in_hospital": null
}"""


_DR_SYSTEM = """You are a clinical NLP expert specialising in Indian diagnostic/laboratory reports.
The text is OCR output from a scanned lab report. Medical abbreviations have been pre-expanded.

Your task: extract ALL test parameters accurately and return a single valid JSON object.
Rules:
  - Treat every row of a results table as one observation.
  - Include the reference range exactly as printed.
  - Mark is_abnormal: true if the result is outside the reference range or flagged H/L/*/↑/↓.
  - For LOINC codes: attempt common mappings (e.g. Hemoglobin → 718-7). Leave null if unsure.
  - Use null for missing fields."""


_DR_SCHEMA = """{
  "document_type": "diagnostic_report",
  "patient": {
    "name": null,
    "age": null,
    "gender": null,
    "id": null,
    "address": null,
    "dob": null
  },
  "laboratory": {
    "name": null,
    "address": null
  },
  "report_date": null,
  "sample_date": null,
  "referring_doctor": null,
  "test_category": null,
  "observations": [
    {
      "parameter": null,
      "result": null,
      "unit": null,
      "reference_range": null,
      "is_abnormal": false,
      "loinc_code": null
    }
  ],
  "interpretation": null,
  "comments": null
}"""


# Groq call with JSON-mode and retry on parse failure / rate limit

def _call_groq_json(system_prompt: str, user_prompt: str, attempt: int = 0) -> dict[str, Any]:
    client = _get_client()
    try:
        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=4096,
        )
        content = response.choices[0].message.content
        return json.loads(content)

    except json.JSONDecodeError as exc:
        if attempt < MAX_RETRIES:
            logger.warning("JSON parse error (attempt %d/%d), retrying in 2 s…", attempt + 1, MAX_RETRIES)
            time.sleep(2)
            return _call_groq_json(system_prompt, user_prompt, attempt + 1)
        raise ValueError(f"LLM returned malformed JSON after {MAX_RETRIES} retries") from exc

    except Exception as exc:
        if attempt < MAX_RETRIES and "rate_limit" in str(exc).lower():
            wait = 10 * (attempt + 1)
            logger.warning("Rate limit hit, waiting %d s…", wait)
            time.sleep(wait)
            return _call_groq_json(system_prompt, user_prompt, attempt + 1)
        raise


# Document classification

def classify_document(expanded_text: str) -> str:
    snippet = expanded_text[:CLASSIFY_SNIPPET_CHARS]
    client = _get_client()
    response = client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": f"Classify this clinical document:\n\n{snippet}"},
        ],
        temperature=0,
        max_tokens=10,
    )
    result = response.choices[0].message.content.strip().lower().replace(".", "")
    if result in ("discharge_summary", "diagnostic_report"):
        return result
    if "discharge" in result:
        return "discharge_summary"
    if any(w in result for w in ("diagnostic", "report", "lab", "laboratory")):
        return "diagnostic_report"
    return "unknown"


# Multi-page chunking

_PAGE_SEP = "\n\n--- PAGE BREAK ---\n\n"


def _chunk_pages(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split page-break-delimited text into chunks ≤ max_chars each."""
    pages = text.split(_PAGE_SEP)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for page in pages:
        page_len = len(page)
        if current and current_len + page_len > max_chars:
            chunks.append(_PAGE_SEP.join(current))
            current = [page]
            current_len = page_len
        else:
            current.append(page)
            current_len += page_len

    if current:
        chunks.append(_PAGE_SEP.join(current))

    return chunks


# Merge helpers for multi-chunk results

def _first_non_null(a: Any, b: Any) -> Any:
    if a is None or (isinstance(a, str) and not a.strip()):
        return b
    return a


def _merge_dict_fields(base: dict, update: dict) -> dict:
    """Fill null fields in base from update."""
    result = dict(base)
    for k, v in update.items():
        if v is not None and result.get(k) is None:
            result[k] = v
        elif isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge_dict_fields(result[k], v)
    return result


def _dedup_observations(obs_list: list[dict]) -> list[dict]:
    """Remove duplicate observations by parameter name (case-insensitive)."""
    seen: set[str] = set()
    deduped: list[dict] = []
    for obs in obs_list:
        key = (obs.get("parameter") or obs.get("test") or "").lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(obs)
        elif not key:
            deduped.append(obs)
    return deduped


def _merge_discharge_summaries(extractions: list[dict]) -> dict:
    if not extractions:
        return {}
    merged = dict(extractions[0])

    for ext in extractions[1:]:
        # Single-value sections: fill null slots from subsequent chunks
        for section in ("patient", "encounter", "vitals"):
            if isinstance(ext.get(section), dict):
                merged[section] = _merge_dict_fields(merged.get(section) or {}, ext[section])

        # Single-value scalar fields
        for field in (
            "treating_doctor", "condition_at_discharge", "follow_up",
            "history_of_present_illness", "past_history", "course_in_hospital",
        ):
            merged[field] = _first_non_null(merged.get(field), ext.get(field))

        # List fields: accumulate across chunks
        for lf in ("diagnoses", "procedures", "chief_complaints", "medications"):
            merged[lf] = (merged.get(lf) or []) + (ext.get(lf) or [])

        # Investigations: accumulate + dedup
        merged["investigations"] = _dedup_observations(
            (merged.get("investigations") or []) + (ext.get("investigations") or [])
        )

    return merged


def _merge_diagnostic_reports(extractions: list[dict]) -> dict:
    if not extractions:
        return {}
    merged = dict(extractions[0])

    for ext in extractions[1:]:
        for section in ("patient", "laboratory"):
            if isinstance(ext.get(section), dict):
                merged[section] = _merge_dict_fields(merged.get(section) or {}, ext[section])

        for field in ("report_date", "sample_date", "referring_doctor",
                      "test_category", "interpretation", "comments"):
            merged[field] = _first_non_null(merged.get(field), ext.get(field))

        merged["observations"] = _dedup_observations(
            (merged.get("observations") or []) + (ext.get("observations") or [])
        )

    return merged


# Single-chunk extractors

def _extract_ds_chunk(text: str) -> dict[str, Any]:
    user_prompt = (
        f"Extract all clinical information from this discharge summary chunk.\n"
        f"Return JSON matching this schema exactly:\n{_DS_SCHEMA}\n\n"
        f"Document text:\n{text}"
    )
    return _call_groq_json(_DS_SYSTEM, user_prompt)


def _extract_dr_chunk(text: str) -> dict[str, Any]:
    user_prompt = (
        f"Extract all clinical information from this diagnostic/lab report chunk.\n"
        f"Return JSON matching this schema exactly:\n{_DR_SCHEMA}\n\n"
        f"Document text:\n{text}"
    )
    return _call_groq_json(_DR_SYSTEM, user_prompt)


# Main extraction dispatcher (single call or chunked)

def _extract_with_chunking(doc_type: str, expanded_text: str) -> dict[str, Any]:
    if len(expanded_text) <= MAX_SINGLE_CALL_CHARS:
        logger.info("Single-call extraction (%d chars).", len(expanded_text))
        if doc_type == "discharge_summary":
            return _extract_ds_chunk(expanded_text)
        else:
            return _extract_dr_chunk(expanded_text)

    chunks = _chunk_pages(expanded_text)
    logger.info("Multi-page extraction: %d chunks from %d chars.", len(chunks), len(expanded_text))

    partials: list[dict] = []
    for i, chunk in enumerate(chunks):
        logger.info("  Extracting chunk %d/%d (%d chars)…", i + 1, len(chunks), len(chunk))
        if doc_type == "discharge_summary":
            partials.append(_extract_ds_chunk(chunk))
        else:
            partials.append(_extract_dr_chunk(chunk))
        # Brief pause between chunks to respect rate limits
        if i < len(chunks) - 1:
            time.sleep(1)

    if doc_type == "discharge_summary":
        return _merge_discharge_summaries(partials)
    else:
        return _merge_diagnostic_reports(partials)


# Public entry point

def extract_clinical_data(
    raw_text: str,
    doc_type_hint: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Full extraction pipeline.
    Returns (document_type, extracted_dict_with_confidence).

    If doc_type_hint is "discharge_summary" or "diagnostic_report", the LLM
    classification step is skipped and that type is used directly.
    """
    logger.info("Pre-processing: expanding medical abbreviations…")
    expanded_text = expand_abbreviations(raw_text)

    if doc_type_hint in ("discharge_summary", "diagnostic_report"):
        logger.info("Document type provided by caller: %s (skipping classification).", doc_type_hint)
        doc_type = doc_type_hint
    else:
        logger.info("Classifying document type…")
        doc_type = classify_document(expanded_text)
        logger.info("Classified as: %s", doc_type)

    logger.info("Extracting structured data…")
    raw_extracted = _extract_with_chunking(
        doc_type if doc_type != "unknown" else "diagnostic_report",
        expanded_text,
    )

    # Pydantic validation + normalisation
    logger.info("Validating extracted data…")
    try:
        if doc_type == "discharge_summary":
            validated = DischargeSummaryData.model_validate(raw_extracted)
            extracted = validated.model_dump()
        elif doc_type == "diagnostic_report":
            validated = DiagnosticReportData.model_validate(raw_extracted)
            extracted = validated.model_dump()
        else:
            extracted = raw_extracted
    except Exception as exc:
        logger.warning("Pydantic validation failed (%s). Using raw LLM output.", exc)
        extracted = raw_extracted

    logger.info("Scoring extraction confidence…")
    confidence = score_confidence(doc_type, extracted, expanded_text, use_llm=True)
    final = annotate_with_confidence(extracted, confidence)

    return doc_type, final
