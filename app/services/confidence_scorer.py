"""
Confidence Scorer
──────────────────
After LLM extraction, scores confidence (0.0–1.0) for each extracted field group
using a lightweight Groq call.

Confidence levels:
  0.0–0.39  → RED   (low — field likely missing or garbled)
  0.40–0.74 → AMBER (medium — partially extracted or uncertain)
  0.75–1.0  → GREEN (high — extracted with good confidence)

The confidence dict is stored alongside the extracted data under key `_confidence`.
Frontend uses these values to render green / amber / red badges per field.
"""

import json
import logging
from typing import Any

from groq import Groq

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client: Groq | None = None

def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=settings.groq_api_key)
    return _client


# ─── Heuristic baseline ───────────────────────────────────────────────────────
# Before calling the LLM, compute a baseline confidence from the extracted data
# itself (null fields → low confidence, non-null → medium baseline).

def _heuristic_confidence(doc_type: str, extracted: dict) -> dict[str, float]:
    """
    Fast, zero-cost baseline confidence using field presence heuristics.
    Used as fallback if the LLM confidence call fails.
    """
    scores: dict[str, float] = {}

    def _field_score(value: Any, is_list: bool = False) -> float:
        if value is None:
            return 0.0
        if is_list:
            return 0.6 if len(value) > 0 else 0.0
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned or cleaned.lower() in ("null", "none", "n/a", "unknown", ""):
                return 0.1
            if len(cleaned) > 3:
                return 0.6
            return 0.3
        if isinstance(value, dict):
            filled = sum(1 for v in value.values() if v and str(v).strip() not in ("null", "none", ""))
            return min(0.6, filled / max(len(value), 1))
        return 0.5

    if doc_type == "discharge_summary":
        scores["patient"] = _field_score(extracted.get("patient"), is_list=False)
        scores["encounter"] = _field_score(extracted.get("encounter"), is_list=False)
        scores["diagnoses"] = _field_score(extracted.get("diagnoses"), is_list=True)
        scores["vitals"] = _field_score(extracted.get("vitals"), is_list=False)
        scores["medications"] = _field_score(extracted.get("medications"), is_list=True)
        scores["investigations"] = _field_score(extracted.get("investigations"), is_list=True)
        scores["chief_complaints"] = _field_score(extracted.get("chief_complaints"), is_list=True)
        scores["history"] = _field_score(extracted.get("history_of_present_illness"))
        scores["procedures"] = _field_score(extracted.get("procedures"), is_list=True)
        scores["treating_doctor"] = _field_score(extracted.get("treating_doctor"))
        scores["condition_at_discharge"] = _field_score(extracted.get("condition_at_discharge"))
        scores["follow_up"] = _field_score(extracted.get("follow_up"))
    else:  # diagnostic_report or unknown
        scores["patient"] = _field_score(extracted.get("patient"), is_list=False)
        scores["laboratory"] = _field_score(extracted.get("laboratory"), is_list=False)
        scores["observations"] = _field_score(extracted.get("observations"), is_list=True)
        scores["test_category"] = _field_score(extracted.get("test_category"))
        scores["report_date"] = _field_score(extracted.get("report_date"))
        scores["referring_doctor"] = _field_score(extracted.get("referring_doctor"))
        scores["interpretation"] = _field_score(extracted.get("interpretation"))

    return scores


# ─── LLM confidence scoring ───────────────────────────────────────────────────

_DS_CONFIDENCE_KEYS = [
    "patient", "encounter", "diagnoses", "vitals", "medications",
    "investigations", "chief_complaints", "history", "procedures",
    "treating_doctor", "condition_at_discharge", "follow_up",
]
_DR_CONFIDENCE_KEYS = [
    "patient", "laboratory", "observations", "test_category",
    "report_date", "referring_doctor", "interpretation",
]

_CONFIDENCE_SYSTEM = """You are a medical data extraction quality assessor.
Given extracted clinical JSON and the source OCR text, rate the extraction
confidence for each field group on a scale 0.0 to 1.0 where:
  1.0 = Extracted clearly and completely from source text
  0.8 = Extracted with high confidence, minor uncertainty
  0.6 = Extracted but may have some inaccuracy
  0.4 = Partially extracted or uncertain
  0.2 = Likely incorrect or very incomplete
  0.0 = Field not found in source / completely missing

Return ONLY a JSON object with field names as keys and float scores as values.
No other text."""


def score_confidence(
    doc_type: str,
    extracted: dict,
    source_text: str,
    use_llm: bool = True,
) -> dict[str, float]:
    """
    Score extraction confidence for each field group.
    Falls back to heuristics if LLM call fails.
    """
    # Always compute heuristic baseline
    baseline = _heuristic_confidence(doc_type, extracted)

    if not use_llm:
        return baseline

    keys = _DS_CONFIDENCE_KEYS if doc_type == "discharge_summary" else _DR_CONFIDENCE_KEYS
    schema_keys = json.dumps({k: 0.0 for k in keys}, indent=2)

    # Use only first 2000 chars of source and 1500 chars of extracted to keep tokens low
    source_snippet = source_text[:2000]
    extracted_snippet = json.dumps(extracted, default=str)[:1500]

    user_prompt = f"""Rate extraction confidence for these fields: {keys}

Return this JSON schema filled with float values 0.0–1.0:
{schema_keys}

Source OCR text (first 2000 chars):
{source_snippet}

Extracted data:
{extracted_snippet}"""

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": _CONFIDENCE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=256,
        )
        raw = json.loads(response.choices[0].message.content)

        # Clamp values to [0.0, 1.0] and fill any missing keys from baseline
        scores: dict[str, float] = {}
        for k in keys:
            raw_val = raw.get(k)
            if isinstance(raw_val, (int, float)):
                scores[k] = max(0.0, min(1.0, float(raw_val)))
            else:
                scores[k] = baseline.get(k, 0.0)

        logger.debug("LLM confidence scores: %s", scores)
        return scores

    except Exception as exc:
        logger.warning("Confidence scoring LLM call failed (%s). Using heuristic baseline.", exc)
        return baseline


def confidence_label(score: float) -> str:
    """Convert numeric score to human-readable label."""
    if score >= 0.75:
        return "high"
    if score >= 0.40:
        return "medium"
    return "low"


def confidence_color(score: float) -> str:
    """Convert numeric score to frontend badge color."""
    if score >= 0.75:
        return "green"
    if score >= 0.40:
        return "amber"
    return "red"


def annotate_with_confidence(extracted: dict, confidence: dict[str, float]) -> dict:
    """
    Return a copy of `extracted` with a `_confidence` key added.
    The frontend reads `_confidence` to render per-field badges.
    """
    result = dict(extracted)
    result["_confidence"] = {
        k: {
            "score": round(v, 2),
            "label": confidence_label(v),
            "color": confidence_color(v),
        }
        for k, v in confidence.items()
    }
    return result
