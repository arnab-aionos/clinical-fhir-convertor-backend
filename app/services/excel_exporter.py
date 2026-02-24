"""
Excel Exporter Service

Generates a human-readable Excel workbook from extracted clinical data
for manual cross-verification before FHIR bundle generation (Stage 2.5).

One workbook per job, one sheet per clinical section.
File naming: {job_id}_{document_type}_{timestamp}.xlsx

Triggered automatically at the end of Stage 2 (after confidence scoring).
FHIR generation is NOT triggered here — it requires an explicit
POST /api/v1/jobs/{id}/generate-fhir call.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

# Style constants

_FILL_HEADER = PatternFill("solid", fgColor="BDD7EE")   # Light blue — header rows
_FILL_HIGH   = PatternFill("solid", fgColor="C6EFCE")   # Green  — HIGH confidence
_FILL_MEDIUM = PatternFill("solid", fgColor="FFEB9C")   # Amber  — MEDIUM confidence
_FILL_LOW    = PatternFill("solid", fgColor="FFC7CE")   # Red    — LOW confidence

_FONT_HEADER  = Font(bold=True)
_FONT_MISSING = Font(color="808080", italic=True)        # Grey italic — NOT EXTRACTED

_TAB_CLINICAL = "4472C4"   # Blue  — clinical data sheets
_TAB_SUMMARY  = "70AD47"   # Green — summary sheet

# Standard 6-column layout used for key-value field sheets
_STANDARD_HEADERS = [
    "Field Name",
    "Extracted Value",
    "Confidence Score",
    "Confidence Level",
    "Reviewer Notes",
    "Verified?",
]

_MIN_COL_WIDTH = 20
_MAX_COL_WIDTH = 60


# Utility helpers

def _red_border() -> Border:
    """Thick red border for FHIR-required fields that are null."""
    s = Side(style="thick", color="FF0000")
    return Border(left=s, right=s, top=s, bottom=s)


def _conf_score(confidence_dict: dict, key: str) -> float:
    """Extract numeric confidence score for a field group."""
    entry = confidence_dict.get(key, {})
    if isinstance(entry, dict):
        return float(entry.get("score", 0.0))
    try:
        return float(entry)
    except (TypeError, ValueError):
        return 0.0


def _conf_level(score: float) -> str:
    if score >= 0.75:
        return "HIGH"
    if score >= 0.40:
        return "MEDIUM"
    return "LOW"


def _conf_fill(level: str) -> PatternFill:
    return {"HIGH": _FILL_HIGH, "MEDIUM": _FILL_MEDIUM, "LOW": _FILL_LOW}.get(
        level, _FILL_LOW
    )


def _is_empty(value: Any) -> bool:
    """Return True if the value should be treated as not-extracted."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in ("", "null", "none", "n/a"):
        return True
    return False


def _display(value: Any) -> str:
    return "NOT EXTRACTED" if _is_empty(value) else str(value)


def _write_standard_header(ws) -> None:
    """Write the standard 6-column header row and freeze pane."""
    for col_idx, header in enumerate(_STANDARD_HEADERS, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = _FONT_HEADER
        cell.fill = _FILL_HEADER
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    ws.freeze_panes = "A2"


def _write_list_header(ws, field_headers: list[str]) -> None:
    """Write a header row for list-based sheets (custom columns)."""
    all_headers = field_headers + ["Confidence Score", "Confidence Level", "Reviewer Notes", "Verified?"]
    for col_idx, header in enumerate(all_headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = _FONT_HEADER
        cell.fill = _FILL_HEADER
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    ws.freeze_panes = "A2"
    return len(all_headers)   # total column count


def _write_field_row(
    ws,
    row_num: int,
    field_name: str,
    value: Any,
    conf_score: float,
    is_fhir_required: bool = False,
) -> None:
    """Write one key-value row in the standard 6-column layout."""
    missing = _is_empty(value)
    level = _conf_level(conf_score)

    ws.cell(row=row_num, column=1, value=field_name)

    b_cell = ws.cell(row=row_num, column=2, value=_display(value))
    if missing:
        b_cell.font = _FONT_MISSING

    ws.cell(row=row_num, column=3, value=round(conf_score, 2))

    d_cell = ws.cell(row=row_num, column=4, value=level)
    d_cell.fill = _conf_fill(level)

    ws.cell(row=row_num, column=5, value="")   # Reviewer Notes
    ws.cell(row=row_num, column=6, value="")   # Verified?

    if is_fhir_required and missing:
        rb = _red_border()
        for col in range(1, 7):
            ws.cell(row=row_num, column=col).border = rb


def _auto_size(ws) -> None:
    """Auto-size all columns to content within [_MIN_COL_WIDTH, _MAX_COL_WIDTH]."""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = max(
            _MIN_COL_WIDTH, min(_MAX_COL_WIDTH, max_len + 4)
        )


# Sheet builders — shared

def _sheet_patient(wb, patient: dict, conf_score: float) -> None:
    ws = wb.create_sheet("Patient Info")
    ws.sheet_properties.tabColor = _TAB_CLINICAL
    _write_standard_header(ws)

    # FHIR R4 required for Patient resource: name and gender
    rows = [
        ("Name",                   patient.get("name"),    True),
        ("Age",                    patient.get("age"),     False),
        ("Gender",                 patient.get("gender"),  True),
        ("Patient ID (UHID/MRD)", patient.get("id"),      False),
        ("Date of Birth",          patient.get("dob"),     False),
        ("Address",                patient.get("address"), False),
    ]
    for row_num, (label, value, required) in enumerate(rows, 2):
        _write_field_row(ws, row_num, label, value, conf_score, is_fhir_required=required)

    _auto_size(ws)


# Sheet builders — Discharge Summary

def _sheet_encounter(wb, encounter: dict, conf_score: float) -> None:
    ws = wb.create_sheet("Encounter Info")
    ws.sheet_properties.tabColor = _TAB_CLINICAL
    _write_standard_header(ws)

    # FHIR R4 required: Encounter.period.start (admission_date)
    rows = [
        ("Admission Date",    encounter.get("admission_date"),   True),
        ("Discharge Date",    encounter.get("discharge_date"),   False),
        ("Department",        encounter.get("department"),       False),
        ("Ward",              encounter.get("ward"),             False),
        ("Bed Number",        encounter.get("bed_number"),       False),
        ("Hospital Name",     encounter.get("hospital_name"),    False),
        ("Hospital Address",  encounter.get("hospital_address"), False),
    ]
    for row_num, (label, value, required) in enumerate(rows, 2):
        _write_field_row(ws, row_num, label, value, conf_score, is_fhir_required=required)

    _auto_size(ws)


def _sheet_diagnoses(wb, diagnoses: list, conf_score: float) -> None:
    ws = wb.create_sheet("Diagnoses")
    ws.sheet_properties.tabColor = _TAB_CLINICAL
    field_headers = ["#", "Diagnosis Text", "Type", "ICD-10 Code"]
    total_cols = _write_list_header(ws, field_headers)

    level = _conf_level(conf_score)
    fill  = _conf_fill(level)

    for row_num, dx in enumerate(diagnoses or [], 2):
        text = dx.get("text")
        missing_text = _is_empty(text)

        values = [
            row_num - 1,
            _display(text),
            dx.get("type") or "",
            dx.get("icd_code") or "",
            round(conf_score, 2),
            level,
            "",   # Reviewer Notes
            "",   # Verified?
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=val)
            if col_idx == 6:
                cell.fill = fill
            if col_idx == 2 and missing_text:
                cell.font = _FONT_MISSING

        # FHIR R4 required: Condition.code (diagnosis text)
        if missing_text:
            rb = _red_border()
            for col in range(1, total_cols + 1):
                ws.cell(row=row_num, column=col).border = rb

    if not diagnoses:
        ws.cell(row=2, column=1, value="No diagnoses extracted")

    _auto_size(ws)


def _sheet_procedures(wb, procedures: list, conf_score: float) -> None:
    ws = wb.create_sheet("Procedures")
    ws.sheet_properties.tabColor = _TAB_CLINICAL
    field_headers = ["#", "Procedure Description", "Date"]
    _write_list_header(ws, field_headers)

    level = _conf_level(conf_score)
    fill  = _conf_fill(level)

    for row_num, proc in enumerate(procedures or [], 2):
        text = proc.get("text")
        values = [
            row_num - 1,
            _display(text),
            proc.get("date") or "",
            round(conf_score, 2),
            level,
            "",
            "",
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=val)
            if col_idx == 5:
                cell.fill = fill
            if col_idx == 2 and _is_empty(text):
                cell.font = _FONT_MISSING

    if not procedures:
        ws.cell(row=2, column=1, value="No procedures extracted")

    _auto_size(ws)


def _sheet_vitals(wb, vitals: dict, conf_score: float) -> None:
    ws = wb.create_sheet("Vitals")
    ws.sheet_properties.tabColor = _TAB_CLINICAL
    _write_standard_header(ws)

    # FHIR R4: any vital that HAS a value becomes an Observation.
    # Observation.value[x] is required. We flag BP as required since it is the
    # most commonly expected vital in NHCX discharge summaries.
    rows = [
        ("Blood Pressure",           vitals.get("bp"),     True),
        ("Pulse / Heart Rate",       vitals.get("pulse"),  False),
        ("Temperature",              vitals.get("temp"),   False),
        ("Oxygen Saturation (SpO2)", vitals.get("spo2"),   False),
        ("Respiratory Rate",         vitals.get("rr"),     False),
        ("Weight",                   vitals.get("weight"), False),
        ("Height",                   vitals.get("height"), False),
    ]
    for row_num, (label, value, required) in enumerate(rows, 2):
        _write_field_row(ws, row_num, label, value, conf_score, is_fhir_required=required)

    _auto_size(ws)


def _sheet_medications(wb, medications: list, conf_score: float) -> None:
    ws = wb.create_sheet("Medications")
    ws.sheet_properties.tabColor = _TAB_CLINICAL
    field_headers = ["#", "Drug Name", "Dosage", "Frequency", "Route", "Duration"]
    total_cols = _write_list_header(ws, field_headers)

    level = _conf_level(conf_score)
    fill  = _conf_fill(level)

    for row_num, med in enumerate(medications or [], 2):
        drug = med.get("drug")
        values = [
            row_num - 1,
            _display(drug),
            med.get("dosage") or "",
            med.get("frequency") or "",
            med.get("route") or "",
            med.get("duration") or "",
            round(conf_score, 2),
            level,
            "",
            "",
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=val)
            if col_idx == 8:
                cell.fill = fill
            if col_idx == 2 and _is_empty(drug):
                cell.font = _FONT_MISSING

    if not medications:
        ws.cell(row=2, column=1, value="No medications extracted")

    _auto_size(ws)


def _sheet_investigations(wb, investigations: list, conf_score: float) -> None:
    ws = wb.create_sheet("Investigations")
    ws.sheet_properties.tabColor = _TAB_CLINICAL
    field_headers = ["#", "Test Name", "Result", "Unit", "Reference Range", "Abnormal?"]
    _write_list_header(ws, field_headers)

    level = _conf_level(conf_score)
    fill  = _conf_fill(level)

    for row_num, inv in enumerate(investigations or [], 2):
        test   = inv.get("test")
        result = inv.get("result")
        is_abnormal = inv.get("is_abnormal", False)

        values = [
            row_num - 1,
            _display(test),
            _display(result),
            inv.get("unit") or "",
            inv.get("ref_range") or "",
            "Abnormal" if is_abnormal else "Normal",
            round(conf_score, 2),
            level,
            "",
            "",
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=val)
            if col_idx == 8:
                cell.fill = fill
            if col_idx == 2 and _is_empty(test):
                cell.font = _FONT_MISSING
            if col_idx == 3 and _is_empty(result):
                cell.font = _FONT_MISSING

    if not investigations:
        ws.cell(row=2, column=1, value="No investigations extracted")

    _auto_size(ws)


# Sheet builders — Diagnostic Report

def _sheet_laboratory(wb, laboratory: dict, conf_score: float) -> None:
    ws = wb.create_sheet("Laboratory Info")
    ws.sheet_properties.tabColor = _TAB_CLINICAL
    _write_standard_header(ws)

    rows = [
        ("Laboratory Name",    laboratory.get("name"),    False),
        ("Laboratory Address", laboratory.get("address"), False),
    ]
    for row_num, (label, value, required) in enumerate(rows, 2):
        _write_field_row(ws, row_num, label, value, conf_score, is_fhir_required=required)

    _auto_size(ws)


def _sheet_observations(wb, observations: list, conf_score: float) -> None:
    ws = wb.create_sheet("Observations")
    ws.sheet_properties.tabColor = _TAB_CLINICAL
    field_headers = [
        "#", "Parameter Name", "Result Value", "Unit",
        "Reference Range", "Abnormal?", "LOINC Code",
    ]
    total_cols = _write_list_header(ws, field_headers)

    level = _conf_level(conf_score)
    fill  = _conf_fill(level)

    for row_num, obs in enumerate(observations or [], 2):
        parameter = obs.get("parameter")
        result    = obs.get("result")
        is_abnormal = obs.get("is_abnormal", False)

        # FHIR R4 required: Observation.code (parameter) AND Observation.value[x] (result)
        missing_required = _is_empty(parameter) or _is_empty(result)

        values = [
            row_num - 1,
            _display(parameter),
            _display(result),
            obs.get("unit") or "",
            obs.get("reference_range") or "",
            "Abnormal" if is_abnormal else "Normal",
            obs.get("loinc_code") or "",
            round(conf_score, 2),
            level,
            "",
            "",
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=val)
            if col_idx == 9:
                cell.fill = fill
            if col_idx == 2 and _is_empty(parameter):
                cell.font = _FONT_MISSING
            if col_idx == 3 and _is_empty(result):
                cell.font = _FONT_MISSING

        if missing_required:
            rb = _red_border()
            for col in range(1, total_cols + 1):
                ws.cell(row=row_num, column=col).border = rb

    if not observations:
        ws.cell(row=2, column=1, value="No observations extracted")

    _auto_size(ws)


# Summary sheet

def _count_fields(data: dict, _skip: frozenset = frozenset(["_confidence", "document_type"])) -> tuple[int, int]:
    """
    Recursively count all leaf (scalar) values.
    Returns (total_fields, missing_fields).
    """
    total = 0
    missing = 0

    def _walk(v: Any) -> None:
        nonlocal total, missing
        if isinstance(v, dict):
            for k, sub in v.items():
                if k in _skip:
                    continue
                _walk(sub)
        elif isinstance(v, list):
            for item in v:
                _walk(item)
        else:
            total += 1
            if _is_empty(v):
                missing += 1

    _walk(data)
    return total, missing


def _sheet_summary(
    wb,
    job_id: str,
    doc_type: str,
    extracted_data: dict,
    confidence_dict: dict,
    timestamp: str,
) -> None:
    ws = wb.create_sheet("Summary")
    ws.sheet_properties.tabColor = _TAB_SUMMARY

    # Calculate overall confidence average
    scores = []
    for v in confidence_dict.values():
        if isinstance(v, dict):
            try:
                scores.append(float(v.get("score", 0.0)))
            except (TypeError, ValueError):
                pass

    avg_conf = round(sum(scores) / len(scores), 2) if scores else 0.0
    total_fields, missing_fields = _count_fields(extracted_data)
    extracted_fields = total_fields - missing_fields

    rows = [
        ("Job ID",                            job_id),
        ("Document Type",                     doc_type.replace("_", " ").title()),
        ("Total Fields Analyzed",             total_fields),
        ("Fields Successfully Extracted",     extracted_fields),
        ("Fields Null / Not Extracted",       missing_fields),
        ("Overall Confidence Score (avg)",    avg_conf),
        ("Overall Confidence Level",          _conf_level(avg_conf)),
        ("Excel Generation Timestamp (UTC)",  timestamp),
        ("Pipeline Stage",                    "AWAITING VERIFICATION"),
        ("FHIR Generation",                   "PENDING — call POST /jobs/{id}/generate-fhir"),
    ]

    for row_num, (key, value) in enumerate(rows, 1):
        k_cell = ws.cell(row=row_num, column=1, value=key)
        k_cell.font = _FONT_HEADER
        ws.cell(row=row_num, column=2, value=value)

    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 55


# Public ExcelExporter class

class ExcelExporter:
    """
    Generates an Excel cross-verification workbook from LLM-extracted clinical data.

    Usage:
        exporter = ExcelExporter(upload_dir=settings.upload_dir)
        file_path = exporter.generate(job_id, document_type, extracted_data)
    """

    def __init__(self, upload_dir: str) -> None:
        self._upload_dir = Path(upload_dir)

    def generate(self, job_id: str, document_type: str, extracted_data: dict) -> str:
        """
        Build and save the Excel workbook.

        `extracted_data` is expected to contain `_confidence` (added by
        annotate_with_confidence() in confidence_scorer.py).

        Returns the absolute file path as a string.
        """
        self._upload_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename  = f"{job_id}_{document_type}_{timestamp}.xlsx"
        file_path = self._upload_dir / filename

        confidence_dict = extracted_data.get("_confidence") or {}

        wb = openpyxl.Workbook()
        wb.remove(wb.active)   # drop the default blank sheet

        if document_type == "discharge_summary":
            self._build_discharge_summary(wb, extracted_data, confidence_dict, job_id, timestamp)
        else:
            # diagnostic_report or unknown — use diagnostic report layout
            self._build_diagnostic_report(wb, extracted_data, confidence_dict, job_id, timestamp)

        wb.save(str(file_path))
        logger.info("Excel workbook saved: %s", file_path)
        return str(file_path)

    # ── Discharge Summary workbook ────────────────────────────────────────────

    def _build_discharge_summary(
        self,
        wb,
        data: dict,
        conf: dict,
        job_id: str,
        timestamp: str,
    ) -> None:
        patient      = data.get("patient")      or {}
        encounter    = data.get("encounter")    or {}
        diagnoses    = data.get("diagnoses")    or []
        procedures   = data.get("procedures")   or []
        vitals       = data.get("vitals")       or {}
        medications  = data.get("medications")  or []
        investigations = data.get("investigations") or []

        _sheet_patient(wb, patient, _conf_score(conf, "patient"))
        _sheet_encounter(wb, encounter, _conf_score(conf, "encounter"))
        _sheet_diagnoses(wb, diagnoses, _conf_score(conf, "diagnoses"))
        _sheet_procedures(wb, procedures, _conf_score(conf, "procedures"))
        _sheet_vitals(wb, vitals, _conf_score(conf, "vitals"))
        _sheet_medications(wb, medications, _conf_score(conf, "medications"))
        _sheet_investigations(wb, investigations, _conf_score(conf, "investigations"))
        _sheet_summary(wb, job_id, "discharge_summary", data, conf, timestamp)

    # ── Diagnostic Report workbook ────────────────────────────────────────────

    def _build_diagnostic_report(
        self,
        wb,
        data: dict,
        conf: dict,
        job_id: str,
        timestamp: str,
    ) -> None:
        patient      = data.get("patient")      or {}
        laboratory   = data.get("laboratory")   or {}
        observations = data.get("observations") or []

        _sheet_patient(wb, patient, _conf_score(conf, "patient"))
        _sheet_laboratory(wb, laboratory, _conf_score(conf, "laboratory"))
        _sheet_observations(wb, observations, _conf_score(conf, "observations"))
        _sheet_summary(wb, job_id, "diagnostic_report", data, conf, timestamp)
