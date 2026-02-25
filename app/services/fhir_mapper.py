"""
FHIR Mapper Service

Converts extracted clinical JSON to FHIR R4 Bundle dicts.

Discharge Summary  → Bundle type "document" with Composition root resource
Diagnostic Report  → Bundle type "collection" with DiagnosticReport root resource

NHCX profile URLs (meta.profile) follow the ABDM NHCX IG:
  https://nrces.in/ndhm/fhir/r4/StructureDefinition/{ResourceType}
"""

import re
import uuid
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# NHCX profile base URL
_NHCX_BASE = "https://nrces.in/ndhm/fhir/r4/StructureDefinition"

# LOINC codes for common vitals
_VITAL_LOINC = {
    "bp": "55284-4",      # Blood pressure systolic & diastolic
    "pulse": "8867-4",    # Heart rate
    "temp": "8310-5",     # Body temperature
    "spo2": "59408-5",    # Oxygen saturation
    "rr": "9279-1",       # Respiratory rate
    "weight": "29463-7",  # Body weight
    "height": "8302-2",   # Body height
}
_VITAL_DISPLAY = {
    "bp": "Blood pressure", "pulse": "Heart rate", "temp": "Body temperature",
    "spo2": "Oxygen saturation", "rr": "Respiratory rate",
    "weight": "Body weight", "height": "Body height",
}
_VITAL_UNIT = {
    "bp": "mmHg", "pulse": "/min", "temp": "degF",
    "spo2": "%", "rr": "/min", "weight": "kg", "height": "cm",
}

# Static LOINC lookup for common Indian clinical lab parameters.
# Keys are lowercase alphanumeric (normalized). Values are LOINC codes.
_LAB_LOINC: dict[str, str] = {
    # Hematology
    "hemoglobin": "718-7", "haemoglobin": "718-7", "hb": "718-7", "hgb": "718-7",
    "wbc": "6690-2", "wbccount": "6690-2", "totalleukocytecount": "6690-2", "tlc": "6690-2",
    "leukocytecount": "6690-2", "totalwbc": "6690-2",
    "platelet": "777-3", "platelets": "777-3", "plateletcount": "777-3", "plt": "777-3",
    "rbc": "789-8", "rbccount": "789-8", "erythrocytecount": "789-8",
    "pcv": "20570-8", "hematocrit": "20570-8", "haematocrit": "20570-8",
    "mcv": "787-2", "mch": "785-6", "mchc": "786-4",
    "neutrophil": "770-8", "neutrophils": "770-8", "polymorphs": "770-8",
    "lymphocyte": "736-9", "lymphocytes": "736-9",
    "monocyte": "5905-5", "monocytes": "5905-5",
    "eosinophil": "713-8", "eosinophils": "713-8",
    "basophil": "706-2", "basophils": "706-2",
    # Biochemistry – sugars
    "glucose": "2345-7", "bloodsugar": "2345-7", "rbs": "2339-0",
    "fbs": "1558-6", "fastingbloodsugar": "1558-6", "fastingglucose": "1558-6",
    "ppbs": "14743-9", "postprandial": "14743-9",
    "hba1c": "4548-4", "glycosylatedhemoglobin": "4548-4", "glycatedhemoglobin": "4548-4",
    # Biochemistry – renal
    "creatinine": "2160-0",
    "bloodurea": "22664-7", "urea": "22664-7", "bun": "3094-0",
    "uricacid": "3084-1",
    # Biochemistry – electrolytes
    "sodium": "2951-2", "sersodsodium": "2951-2",
    "potassium": "2823-3",
    "chloride": "2075-0",
    "bicarbonate": "1963-8", "hco3": "1963-8",
    # Biochemistry – liver
    "totalbilirubin": "1975-2", "bilirubin": "1975-2",
    "directbilirubin": "1968-7", "conjugatedbilirubin": "1968-7",
    "indirectbilirubin": "1971-1",
    "sgot": "1920-8", "ast": "1920-8",
    "sgpt": "1742-6", "alt": "1742-6",
    "alp": "6768-6", "alkalinephosphatase": "6768-6",
    "ggt": "2324-2", "gammaglutamyltransferase": "2324-2",
    "totalprotein": "2885-2",
    "albumin": "1751-7",
    "globulin": "10834-0",
    # Biochemistry – minerals
    "calcium": "17861-6",
    "phosphorus": "2777-1", "phosphate": "2777-1",
    "magnesium": "2601-3",
    # Biochemistry – lipids
    "totalcholesterol": "2093-3", "cholesterol": "2093-3",
    "triglycerides": "2571-8", "triglyceride": "2571-8",
    "hdlcholesterol": "2085-9", "hdl": "2085-9",
    "ldlcholesterol": "18262-6", "ldl": "18262-6",
    "vldl": "13457-7",
    # Thyroid
    "tsh": "3016-3",
    "t3": "3053-6",
    "t4": "3026-2",
    "freet4": "3024-7", "ft4": "3024-7",
    "freet3": "3051-0", "ft3": "3051-0",
    # Urinalysis
    "urineprotein": "2888-6",
    "urineglucose": "25428-4",
    "urinespecificgravity": "2965-2", "specificgravity": "2965-2",
    "urineph": "2756-5",
    # Cardiac markers
    "troponin": "10839-9", "troponini": "10839-9", "tni": "10839-9",
    "troponint": "6598-7", "tnt": "6598-7",
    "ckmb": "13969-1",
    "bnp": "30934-4",
    "ntprobnp": "33762-6",
    # Coagulation
    "pt": "5902-2", "prothrombintime": "5902-2",
    "aptt": "3173-2", "ptt": "3173-2",
    "inr": "6301-6",
    # Inflammation / infection
    "crp": "1988-5", "creactiveprotein": "1988-5",
    "esr": "30341-2", "erythrocytesedimentationrate": "30341-2",
    "procalcitonin": "33959-8", "pct": "33959-8",
    # Micronutrients
    "ferritin": "2276-4",
    "vitaminb12": "2132-9",
    "vitamind": "14635-7", "25ohvitamind": "14635-7",
    "folate": "2284-8", "folicacid": "2284-8",
    # Oncology
    "psa": "10508-0",
    "cea": "2857-1",
    "ca125": "10334-7",
    "afp": "1834-1",
}


def _lookup_loinc(param_name: str) -> str:
    """
    Look up a LOINC code for a lab parameter using the static _LAB_LOINC table.
    Normalises by lowercasing, stripping non-alphanumeric chars, and removing
    common prefix words (serum, blood, plasma, fasting, random, urine).
    Returns the LOINC code string or "" if not found.
    """
    if not param_name:
        return ""
    normalized = re.sub(r"[^a-z0-9]", "", param_name.lower())
    # Strip common uninformative prefixes
    for prefix in ("serum", "blood", "plasma", "fasting", "random", "urine"):
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            normalized = normalized[len(prefix):]
    # Exact match
    if normalized in _LAB_LOINC:
        return _LAB_LOINC[normalized]
    # Substring match — key contained in normalized input (e.g. "sgpt" in "sgptalat")
    for key, code in _LAB_LOINC.items():
        if len(key) >= 3 and key in normalized:
            return code
    return ""


def _uid() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_fhir_date(raw: Optional[str]) -> Optional[str]:
    """
    Normalise a raw date/datetime string extracted by the LLM into an
    ISO 8601 FHIR-compatible date (YYYY-MM-DD or YYYY-MM-DDThh:mm:ssZ).

    Handles common Indian/clinical formats:
      DD/MM/YYYY [H:MMam/pm]  → YYYY-MM-DDThh:mm:ssZ
      DD-MM-YYYY              → YYYY-MM-DD
      YYYY-MM-DD              → returned as-is
      Already valid ISO       → returned as-is

    Returns None if the string cannot be parsed.
    """
    if not raw:
        return None
    raw = raw.strip()
    # Already looks like YYYY-... (ISO-ish)
    if len(raw) >= 4 and raw[:4].isdigit() and raw[4:5] in ("-", "T", ""):
        return raw

    from datetime import datetime as dt
    formats = [
        "%d/%m/%Y %I:%M%p",   # 15/03/2025 1:28PM
        "%d/%m/%Y %I:%M %p",  # 15/03/2025 1:28 PM
        "%d/%m/%Y %H:%M",     # 15/03/2025 13:28
        "%d/%m/%Y",            # 15/03/2025
        "%d-%m-%Y",            # 15-03-2025
        "%d %B %Y",            # 15 March 2025
        "%B %d, %Y",           # March 15, 2025
        "%d/%m/%y",            # 15/03/25
    ]
    for fmt in formats:
        try:
            parsed = dt.strptime(raw, fmt)
            if "%H" in fmt or "%I" in fmt:
                return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    logger.debug("Could not parse date %r — omitting from FHIR resource", raw)
    return None


def _profile(name: str) -> list[dict]:
    return [{"url": f"{_NHCX_BASE}/{name}"}]


def _meta(profile_name: str) -> dict:
    return {"profile": [f"{_NHCX_BASE}/{profile_name}"]}


# Resource builders

def _build_patient(patient: dict) -> dict:
    name_text = patient.get("name") or "Unknown"
    resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": _uid(),
        "meta": _meta("NHCXPatient"),
        "name": [{"text": name_text}],
    }
    if patient.get("gender"):
        g = patient["gender"].lower()
        if g in ("male", "m"):
            resource["gender"] = "male"
        elif g in ("female", "f"):
            resource["gender"] = "female"
        else:
            resource["gender"] = "unknown"
    if patient.get("dob"):
        dob = _to_fhir_date(patient["dob"])
        if dob:
            resource["birthDate"] = dob
    if patient.get("id"):
        resource["identifier"] = [{"value": patient["id"], "system": "urn:nhcx:patient-id"}]
    if patient.get("address"):
        resource["address"] = [{"text": patient["address"]}]
    return resource


def _build_organization(name: Optional[str], address: Optional[str], profile: str) -> dict:
    resource: dict[str, Any] = {
        "resourceType": "Organization",
        "id": _uid(),
        "meta": _meta(profile),
        "name": name or "Unknown Organization",
    }
    # R5: address moved inside contact[].address
    if address:
        resource["contact"] = [{"address": {"text": address}}]
    return resource


def _build_practitioner(name: Optional[str]) -> Optional[dict]:
    if not name:
        return None
    return {
        "resourceType": "Practitioner",
        "id": _uid(),
        "meta": _meta("NHCXPractitioner"),
        "name": [{"text": name}],
    }


def _build_encounter(encounter: dict, patient_ref: str, org_ref: Optional[str]) -> dict:
    resource: dict[str, Any] = {
        "resourceType": "Encounter",
        "id": _uid(),
        "meta": _meta("NHCXEncounter"),
        "status": "finished",
        # R5: class is array of CodeableConcept (was single Coding in R4)
        "class": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP", "display": "inpatient encounter"}]}],
        "subject": {"reference": patient_ref},
    }
    period: dict[str, str] = {}
    if encounter.get("admission_date"):
        d = _to_fhir_date(encounter["admission_date"])
        if d:
            period["start"] = d
    if encounter.get("discharge_date"):
        d = _to_fhir_date(encounter["discharge_date"])
        if d:
            period["end"] = d
    if period:
        # R5: Encounter.period renamed to actualPeriod
        resource["actualPeriod"] = period
    if encounter.get("department") or encounter.get("ward"):
        loc_parts = [p for p in [encounter.get("department"), encounter.get("ward")] if p]
        resource["location"] = [{"location": {"display": " / ".join(loc_parts)}}]
    if org_ref:
        resource["serviceProvider"] = {"reference": org_ref}
    return resource


def _build_condition(diagnosis: dict, patient_ref: str, encounter_ref: str) -> dict:
    code_text = diagnosis.get("text") or "Unspecified condition"
    coding: list[dict] = []
    if diagnosis.get("icd_code"):
        coding.append({
            "system": "http://hl7.org/fhir/sid/icd-10",
            "code": diagnosis["icd_code"],
            "display": code_text,
        })

    return {
        "resourceType": "Condition",
        "id": _uid(),
        "meta": _meta("NHCXCondition"),
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]
        },
        "verificationStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]
        },
        "code": {"coding": coding, "text": code_text},
        "subject": {"reference": patient_ref},
        "encounter": {"reference": encounter_ref},
    }


def _build_procedure(procedure: dict, patient_ref: str, encounter_ref: str) -> dict:
    resource: dict[str, Any] = {
        "resourceType": "Procedure",
        "id": _uid(),
        "meta": _meta("NHCXProcedure"),
        "status": "completed",
        "code": {"text": procedure.get("text") or "Unspecified procedure"},
        "subject": {"reference": patient_ref},
        "encounter": {"reference": encounter_ref},
    }
    if procedure.get("date"):
        d = _to_fhir_date(procedure["date"])
        if d:
            # R5: performedDateTime renamed to occurrenceDateTime
            resource["occurrenceDateTime"] = d
    return resource


def _build_vital_observation(vital_key: str, value: str, patient_ref: str, encounter_ref: str) -> dict:
    loinc = _VITAL_LOINC.get(vital_key, "")
    display = _VITAL_DISPLAY.get(vital_key, vital_key)
    return {
        "resourceType": "Observation",
        "id": _uid(),
        "meta": _meta("NHCXObservation"),
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs", "display": "Vital Signs"}]}],
        "code": {
            "coding": [{"system": "http://loinc.org", "code": loinc, "display": display}] if loinc else [],
            "text": display,
        },
        "subject": {"reference": patient_ref},
        "encounter": {"reference": encounter_ref},
        "effectiveDateTime": _now_iso(),
        "valueString": value,
    }


def _build_lab_observation(investigation: dict, patient_ref: str, encounter_ref: str) -> dict:
    param = investigation.get("test") or investigation.get("parameter") or "Unknown test"
    # Priority: LLM-provided code → static lookup → empty (coding array omitted)
    loinc_code = investigation.get("loinc_code") or ""
    if not loinc_code:
        loinc_code = _lookup_loinc(param)
    obs: dict[str, Any] = {
        "resourceType": "Observation",
        "id": _uid(),
        "meta": _meta("NHCXObservation"),
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory", "display": "Laboratory"}]}],
        "code": {
            "coding": [{"system": "http://loinc.org", "code": loinc_code, "display": param}] if loinc_code else [],
            "text": param,
        },
        "subject": {"reference": patient_ref},
        "effectiveDateTime": _now_iso(),
    }
    # Only add encounter reference when it is a real non-empty reference string
    if encounter_ref:
        obs["encounter"] = {"reference": encounter_ref}
    result = investigation.get("result")
    if result:
        try:
            vq: dict[str, Any] = {
                "value": float(result),
                "system": "http://unitsofmeasure.org",
            }
            unit = investigation.get("unit") or ""
            if unit:
                vq["unit"] = unit
            obs["valueQuantity"] = vq
        except (ValueError, TypeError):
            obs["valueString"] = str(result)
            if investigation.get("unit"):
                obs["valueString"] += f" {investigation['unit']}"
    ref = investigation.get("ref_range") or investigation.get("reference_range")
    if ref:
        obs["referenceRange"] = [{"text": ref}]
    if investigation.get("is_abnormal"):
        obs["interpretation"] = [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": "A", "display": "Abnormal"}]}]
    return obs


def _build_medication_statement(med: dict, patient_ref: str) -> dict:
    resource: dict[str, Any] = {
        "resourceType": "MedicationStatement",
        "id": _uid(),
        "meta": _meta("NHCXMedicationStatement"),
        "status": "recorded",
        # R5: medication is CodeableReference — use concept for coded text
        "medication": {"concept": {"text": med.get("drug") or med.get("name") or "Unknown medication"}},
        "subject": {"reference": patient_ref},
    }
    # Only add dosage when there is actual text — FHIR schema rejects empty string
    dosage_text = " ".join(filter(None, [
        med.get("dosage"), med.get("frequency"), med.get("duration"), med.get("route")
    ]))
    if dosage_text:
        resource["dosage"] = [{"text": dosage_text}]
    return resource


def _ref(resource: dict) -> str:
    return f"{resource['resourceType']}/{resource['id']}"


def _entry(resource: dict, fullUrl: Optional[str] = None) -> dict:
    e: dict[str, Any] = {"resource": resource}
    if fullUrl:
        e["fullUrl"] = fullUrl
    else:
        e["fullUrl"] = f"urn:uuid:{resource['id']}"
    return e


# Discharge Summary → FHIR document Bundle

def map_discharge_summary(data: dict) -> dict:
    patient_data = data.get("patient") or {}
    encounter_data = data.get("encounter") or {}

    patient = _build_patient(patient_data)
    hospital_name = encounter_data.get("hospital_name") or "Hospital"
    hospital_address = encounter_data.get("hospital_address")
    org = _build_organization(hospital_name, hospital_address, "NHCXOrganization")
    practitioner = _build_practitioner(data.get("treating_doctor"))
    encounter = _build_encounter(encounter_data, _ref(patient), _ref(org))

    conditions = [
        _build_condition(d, _ref(patient), _ref(encounter))
        for d in (data.get("diagnoses") or [])
    ]
    procedures = [
        _build_procedure(p, _ref(patient), _ref(encounter))
        for p in (data.get("procedures") or [])
    ]

    # Vitals observations
    vitals_obs: list[dict] = []
    for key, val in (data.get("vitals") or {}).items():
        if val and key in _VITAL_LOINC:
            vitals_obs.append(_build_vital_observation(key, str(val), _ref(patient), _ref(encounter)))

    # Lab investigations from the summary
    lab_obs: list[dict] = []
    for inv in (data.get("investigations") or []):
        if inv.get("test"):
            lab_obs.append(_build_lab_observation(inv, _ref(patient), _ref(encounter)))

    med_statements = [
        _build_medication_statement(m, _ref(patient))
        for m in (data.get("medications") or [])
    ]

    # Build Composition sections
    sections: list[dict] = []
    if conditions:
        sections.append({
            "title": "Diagnoses",
            "code": {"coding": [{"system": "http://loinc.org", "code": "11450-4", "display": "Problem list"}]},
            "entry": [{"reference": _ref(c)} for c in conditions],
        })
    if procedures:
        sections.append({
            "title": "Procedures",
            "code": {"coding": [{"system": "http://loinc.org", "code": "47519-4", "display": "History of procedures"}]},
            "entry": [{"reference": _ref(p)} for p in procedures],
        })
    if vitals_obs:
        sections.append({
            "title": "Vital Signs",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8716-3", "display": "Vital signs"}]},
            "entry": [{"reference": _ref(o)} for o in vitals_obs],
        })
    if lab_obs:
        sections.append({
            "title": "Investigations",
            "code": {"coding": [{"system": "http://loinc.org", "code": "30954-2", "display": "Relevant diagnostic tests"}]},
            "entry": [{"reference": _ref(o)} for o in lab_obs],
        })
    if med_statements:
        sections.append({
            "title": "Medications",
            "code": {"coding": [{"system": "http://loinc.org", "code": "10160-0", "display": "Medication use"}]},
            "entry": [{"reference": _ref(m)} for m in med_statements],
        })

    # Chief complaints & narrative as text sections
    if data.get("chief_complaints"):
        sections.append({
            "title": "Chief Complaints",
            "code": {"coding": [{"system": "http://loinc.org", "code": "10154-3", "display": "Chief complaint"}]},
            "text": {"status": "generated", "div": f"<div xmlns='http://www.w3.org/1999/xhtml'>{'; '.join(data['chief_complaints'])}</div>"},
        })

    # Composition.author is required (1..*) in FHIR R4.
    # Use practitioner if available, otherwise fall back to the hospital organisation.
    composition_author = (
        [{"reference": _ref(practitioner)}]
        if practitioner
        else [{"reference": _ref(org)}]
    )
    composition: dict[str, Any] = {
        "resourceType": "Composition",
        "id": _uid(),
        "meta": _meta("NHCXComposition"),
        "status": "final",
        "type": {
            "coding": [{"system": "http://loinc.org", "code": "18842-5", "display": "Discharge summary"}],
            "text": "Discharge Summary",
        },
        "date": _now_iso(),
        # R5: Composition.subject is array (was single Reference in R4)
        "subject": [{"reference": _ref(patient)}],
        "encounter": {"reference": _ref(encounter)},
        "author": composition_author,
        "title": "Discharge Summary",
        "section": sections,
    }

    # Assemble entries
    all_resources = [composition, patient, org, encounter]
    if practitioner:
        all_resources.append(practitioner)
    all_resources += conditions + procedures + vitals_obs + lab_obs + med_statements

    bundle = {
        "resourceType": "Bundle",
        "id": _uid(),
        "meta": _meta("NHCXBundle"),
        "type": "document",
        "timestamp": _now_iso(),
        "entry": [_entry(r) for r in all_resources],
    }
    return bundle


# Diagnostic Report → FHIR collection Bundle

def map_diagnostic_report(data: dict) -> dict:
    patient_data = data.get("patient") or {}
    lab_data = data.get("laboratory") or {}

    patient = _build_patient(patient_data)
    lab_org = _build_organization(lab_data.get("name"), lab_data.get("address"), "NHCXOrganization")
    ref_practitioner = _build_practitioner(data.get("referring_doctor"))

    observations = [
        _build_lab_observation(obs, _ref(patient), "")
        for obs in (data.get("observations") or [])
        if obs.get("parameter")
    ]

    diag_report: dict[str, Any] = {
        "resourceType": "DiagnosticReport",
        "id": _uid(),
        "meta": _meta("NHCXDiagnosticReport"),
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB", "display": "Laboratory"}]}],
        "code": {
            "text": data.get("test_category") or "Laboratory Report",
            "coding": [{"system": "http://loinc.org", "code": "11502-2", "display": "Laboratory report"}],
        },
        "subject": {"reference": _ref(patient)},
        "effectiveDateTime": _to_fhir_date(data.get("report_date")) or _now_iso(),
        "issued": _now_iso(),
        "performer": [{"reference": _ref(lab_org)}],
        "result": [{"reference": _ref(o)} for o in observations],
    }
    if data.get("interpretation"):
        diag_report["conclusion"] = data["interpretation"]

    all_resources = [diag_report, patient, lab_org]
    if ref_practitioner:
        all_resources.append(ref_practitioner)
        diag_report["resultsInterpreter"] = [{"reference": _ref(ref_practitioner)}]
    all_resources += observations

    bundle = {
        "resourceType": "Bundle",
        "id": _uid(),
        "meta": _meta("NHCXBundle"),
        "type": "collection",
        "timestamp": _now_iso(),
        "entry": [_entry(r) for r in all_resources],
    }
    return bundle


# Public entry point

def generate_fhir_bundle(document_type: str, extracted_data: dict) -> dict:
    if document_type == "discharge_summary":
        return map_discharge_summary(extracted_data)
    elif document_type == "diagnostic_report":
        return map_diagnostic_report(extracted_data)
    else:
        # Best-effort: try diagnostic report mapping
        logger.warning("Unknown document type '%s'; attempting diagnostic report mapping.", document_type)
        return map_diagnostic_report(extracted_data)
