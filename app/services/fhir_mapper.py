"""
FHIR Mapper Service

Converts extracted clinical JSON to FHIR R4 Bundle dicts.

Discharge Summary  → Bundle type "document" with Composition root resource
Diagnostic Report  → Bundle type "collection" with DiagnosticReport root resource

Profile URLs follow the NRCeS ABDM FHIR R4 Implementation Guide (v6.5.0):
  https://nrces.in/ndhm/fhir/r4/StructureDefinition/{ResourceType}

Use case: NHCX Claim Submission — supporting clinical documents (discharge
summaries and diagnostic/lab reports) converted to FHIR R4 bundles for
submission under the National Health Claims Exchange (NHCX) framework.
"""

import re
import uuid
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# NRCeS ABDM FHIR R4 IG profile base URL — loaded from config so it can be
# overridden via NHCX_PROFILE_BASE_URL env var without touching source code.
_NHCX_BASE: str = get_settings().nhcx_profile_base_url

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
    # Anthropometrics (often captured as investigations in discharge summaries)
    "weight": "29463-7", "bodyweight": "29463-7",
    "height": "8302-2", "bodyheight": "8302-2",
    "bmi": "39156-5", "bodymassindex": "39156-5",
    "waistcircumference": "56115-9",
    "headcircumference": "9843-4",
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
    # Blood group / transfusion
    "bloodgroup": "883-9", "bloodgrouping": "883-9", "abobloodgroup": "883-9", "abogroup": "883-9",
    "rhtype": "1305-0", "rhfactor": "1305-0", "bloodrh": "1305-0", "rh": "1305-0",
    # Cytology / histopathology
    "liquidbasedcytology": "19765-7", "lbc": "19765-7", "liquidcytology": "19765-7",
    "fnac": "47527-7", "fineneedleaspirationcytology": "47527-7", "fineneedleaspiration": "47527-7",
    "exfoliativecytology": "19756-6", "exfoliative": "19756-6",
    "papanicola": "19762-4", "papsmear": "19762-4", "cervicalcytology": "19762-4",
}


# Static ICD-10 lookup for common Indian clinical diagnoses.
# Keys are lowercase alphanumeric (normalized). Values are ICD-10 codes.
_CONDITION_ICD: dict[str, str] = {
    # Cardiovascular
    "hypertension": "I10", "essentialhypertension": "I10", "htn": "I10",
    "heartfailure": "I50", "congestiveheartfailure": "I50.0", "chf": "I50.0",
    "myocardialinfarction": "I21", "acutemi": "I21", "mi": "I21",
    "stemi": "I21.0", "nstemi": "I21.4",
    "coronaryarterydisease": "I25.1", "cad": "I25.1",
    "ischemicheartdisease": "I25", "ihd": "I25",
    "atrialfibrillation": "I48", "af": "I48",
    "anginapectoris": "I20", "angina": "I20",
    "deepveinthrombosis": "I82.4", "dvt": "I82.4",
    "pulmonaryembolism": "I26",
    "cardiacarrest": "I46",
    # Diabetes
    "diabetesmellitus": "E11", "type2diabetes": "E11", "t2dm": "E11", "dm": "E11",
    "type1diabetes": "E10", "t1dm": "E10",
    "diabeticnephropathy": "E11.2", "diabeticretinopathy": "E11.3",
    "diabeticneuropathy": "E11.4",
    # Respiratory
    "pneumonia": "J18", "bacterialpneumonia": "J15",
    "covid19": "U07.1", "covid": "U07.1", "sarscov2": "U07.1",
    "copd": "J44", "chronicobstructivepulmonarydisease": "J44",
    "asthma": "J45", "bronchialasthma": "J45",
    "pulmonarytuberculosis": "A15", "tuberculosis": "A15", "tb": "A15",
    "pleuraleffusion": "J90",
    "acuterespiratorydistresssyndrome": "J80", "ards": "J80",
    "bronchitis": "J40", "acutebronchitis": "J20",
    # Gastrointestinal
    "appendicitis": "K37", "acuteappendicitis": "K37",
    "cholecystitis": "K81", "acutecholecystitis": "K81.0",
    "cholelithiasis": "K80", "gallstones": "K80",
    "pancreatitis": "K85", "acutepancreatitis": "K85", "chronicpancreatitis": "K86.1",
    "gastroenteritis": "A09",
    "pepticulcer": "K27", "gastriculcer": "K25", "duodenalulcer": "K26",
    "gerd": "K21", "gastrooesophagealreflux": "K21",
    "cirrhosis": "K74.6", "livercirrhosis": "K74.6",
    "hepatitis": "K75.9", "hepatitisb": "B18.1", "hepatitisc": "B18.2", "hepatitisa": "B15.9",
    "intestinalobstruction": "K56",
    # Neurological
    "stroke": "I64", "cerebrovasculardisease": "I67",
    "cerebralinfraction": "I63", "ischemicstroke": "I63",
    "hemorrhagicstroke": "I61", "intracerebralhemorrhage": "I61",
    "epilepsy": "G40", "seizure": "G40",
    "meningitis": "G03", "bacterialmeningitis": "G00", "encephalitis": "G04",
    "parkinsons": "G20", "parkinsonsdisease": "G20",
    "alzheimer": "G30", "alzheimerdisease": "G30", "dementia": "F03",
    "migraine": "G43",
    # Renal
    "acutekidneyinjury": "N17", "aki": "N17",
    "chronickidneydisease": "N18", "ckd": "N18",
    "renalcalculi": "N20", "kidneystones": "N20",
    "urinarytractinfection": "N39.0", "uti": "N39.0",
    "nephroticsyndrome": "N04", "glomerulonephritis": "N05", "pyelonephritis": "N12",
    # Infectious diseases
    "typhoid": "A01.0", "typhoidfever": "A01.0",
    "malaria": "B54", "dengue": "A97", "denguefever": "A97",
    "chickenpox": "B01", "varicella": "B01",
    "hiv": "B24", "aids": "B24",
    "sepsis": "A41.9", "septicshock": "A41.9",
    # Musculoskeletal
    "rheumatoidarthritis": "M05", "osteoarthritis": "M19",
    "gout": "M10", "osteoporosis": "M81",
    "backpain": "M54.5", "lumbarpain": "M54.5",
    "cervicalspondylosis": "M47.8",
    "hipfracture": "S72",
    # Oncology
    "lungcancer": "C34", "breastcancer": "C50",
    "coloncancer": "C18", "colorectalcancer": "C20",
    "prostatecancer": "C61", "cervicalcancer": "C53",
    "lymphoma": "C85", "leukemia": "C95",
    "gastriccancer": "C16",
    # Mental health
    "depression": "F32", "majordepression": "F32",
    "anxiety": "F41", "anxietydisorder": "F41",
    "schizophrenia": "F20", "bipolardisorder": "F31",
    # Endocrine
    "hypothyroidism": "E03", "hyperthyroidism": "E05", "thyrotoxicosis": "E05",
    "obesity": "E66",
    # Obstetrics / Gynaecology
    "preeclampsia": "O14", "eclampsia": "O15",
    "gestationaldiabetes": "O24",
    # More specific OB delivery codes first — substring match returns the first hit
    "vacuumdelivery": "O81.4", "vacuumassisted": "O81.4", "vacuumassisteddelivery": "O81.4", "vaccum": "O81.4",
    "forcepsdelivery": "O81.3",
    "cesareansection": "O82", "lscs": "O82", "caesarean": "O82",
    "preterm": "O60", "pretermdelivery": "O60", "pretermbirth": "O60",
    # Generic delivery codes after specific ones
    "normaldelivery": "O80", "normalvaginaldelivery": "O80", "vaginaldelivery": "O80",
    "fullterm": "O80",
    "anemia": "D64", "irondeficiencyanemia": "D50",
    # Commonly documented signs / syndromes
    "fever": "R50", "pyrexia": "R50",
    "dehydration": "E86",
    "electrolytedisturbance": "E87",
    "malnutrition": "E46",
    "shock": "R57",
    "cellulitis": "L03",
}


def _lookup_icd(diagnosis_text: str) -> str:
    """
    Look up an ICD-10 code for a diagnosis using the static _CONDITION_ICD table.
    Normalises by lowercasing and stripping non-alphanumeric characters.
    Returns the ICD-10 code string or "" if not found.
    """
    if not diagnosis_text:
        return ""
    normalized = re.sub(r"[^a-z0-9]", "", diagnosis_text.lower())
    # Exact match
    if normalized in _CONDITION_ICD:
        return _CONDITION_ICD[normalized]
    # Substring match — key contained in normalized text (e.g. "htn" in "htnwithckd")
    for key, code in _CONDITION_ICD.items():
        if len(key) >= 3 and key in normalized:
            return code
    return ""


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
    # Exact match BEFORE prefix stripping (preserves compound names like "bloodgrouping")
    if normalized in _LAB_LOINC:
        return _LAB_LOINC[normalized]
    # Strip common uninformative prefixes then try exact match again
    for prefix in ("serum", "blood", "plasma", "fasting", "random", "urine"):
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            normalized = normalized[len(prefix):]
            break
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


def _meta(profile_name: str) -> dict:
    """Return FHIR meta with NRCeS ABDM R4 IG canonical profile URL."""
    return {"profile": [f"{_NHCX_BASE}/{profile_name}"]}


# Resource builders

def _build_patient(patient: dict) -> dict:
    name_text = patient.get("name") or "Unknown"
    resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": _uid(),
        "meta": _meta("Patient"),
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


def _build_organization(name: Optional[str], address: Optional[str]) -> dict:
    resource: dict[str, Any] = {
        "resourceType": "Organization",
        "id": _uid(),
        "meta": _meta("Organization"),
        "name": name or "Unknown Organization",
    }
    # FHIR R4: address is a direct array on Organization
    if address:
        resource["address"] = [{"text": address}]
    return resource


def _build_practitioner(name: Optional[str]) -> Optional[dict]:
    if not name:
        return None
    return {
        "resourceType": "Practitioner",
        "id": _uid(),
        "meta": _meta("Practitioner"),
        "name": [{"text": name}],
    }


def _build_encounter(encounter: dict, patient_ref: str, org_ref: Optional[str]) -> dict:
    resource: dict[str, Any] = {
        "resourceType": "Encounter",
        "id": _uid(),
        "meta": _meta("Encounter"),
        "status": "finished",
        # FHIR R4: class is a single Coding (not array)
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "IMP",
            "display": "inpatient encounter",
        },
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
        # FHIR R4: Encounter.period (R5 renamed this to actualPeriod)
        resource["period"] = period
    if encounter.get("department") or encounter.get("ward"):
        loc_parts = [p for p in [encounter.get("department"), encounter.get("ward")] if p]
        resource["location"] = [{"location": {"display": " / ".join(loc_parts)}}]
    if org_ref:
        resource["serviceProvider"] = {"reference": org_ref}
    return resource


def _build_condition(diagnosis: dict, patient_ref: str, encounter_ref: str) -> dict:
    code_text = diagnosis.get("text") or "Unspecified condition"
    coding: list[dict] = []
    # Use ICD-10 code from LLM output; fall back to static lookup table
    icd_code = diagnosis.get("icd_code") or _lookup_icd(code_text)
    if icd_code:
        coding.append({
            "system": "http://hl7.org/fhir/sid/icd-10",
            "code": icd_code,
            "display": code_text,
        })

    return {
        "resourceType": "Condition",
        "id": _uid(),
        "meta": _meta("Condition"),
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
        "meta": _meta("Procedure"),
        "status": "completed",
        "code": {"text": procedure.get("text") or "Unspecified procedure"},
        "subject": {"reference": patient_ref},
        "encounter": {"reference": encounter_ref},
    }
    if procedure.get("date"):
        d = _to_fhir_date(procedure["date"])
        if d:
            # FHIR R4: performedDateTime (R5 renamed to occurrenceDateTime)
            resource["performedDateTime"] = d
    return resource


def _build_vital_observation(vital_key: str, value: str, patient_ref: str, encounter_ref: str) -> dict:
    loinc = _VITAL_LOINC.get(vital_key, "")
    display = _VITAL_DISPLAY.get(vital_key, vital_key)
    return {
        "resourceType": "Observation",
        "id": _uid(),
        "meta": _meta("Observation"),
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
        "meta": _meta("Observation"),
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
        "meta": _meta("MedicationStatement"),
        "status": "active",
        # FHIR R4: medication[x] choice — medicationCodeableConcept for inline text
        "medicationCodeableConcept": {
            "text": med.get("drug") or med.get("name") or "Unknown medication"
        },
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


# Discharge Summary → FHIR document Bundle (NHCX Claim Submission)

def map_discharge_summary(data: dict) -> dict:
    patient_data = data.get("patient") or {}
    encounter_data = data.get("encounter") or {}

    patient = _build_patient(patient_data)
    hospital_name = encounter_data.get("hospital_name") or "Hospital"
    hospital_address = encounter_data.get("hospital_address")
    org = _build_organization(hospital_name, hospital_address)
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
        "meta": _meta("Composition"),
        "status": "final",
        "type": {
            "coding": [{"system": "http://loinc.org", "code": "18842-5", "display": "Discharge summary"}],
            "text": "Discharge Summary",
        },
        "date": _now_iso(),
        # FHIR R4: Composition.subject is a single Reference (not array)
        "subject": {"reference": _ref(patient)},
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
        "meta": {
            "profile": [f"{_NHCX_BASE}/Bundle"],
            # NHCX claim submission tag — identifies this bundle as a supporting
            # clinical document for NHCX health insurance claim submission
            "tag": [
                {
                    "system": "https://nhcx.abdm.gov.in/fhir/CodeSystem/bundle-use-case",
                    "code": "claim-submission",
                    "display": "NHCX Claim Submission",
                }
            ],
        },
        "type": "document",
        "timestamp": _now_iso(),
        "entry": [_entry(r) for r in all_resources],
    }
    return bundle


# Diagnostic Report → FHIR collection Bundle (NHCX Claim Submission)

def map_diagnostic_report(data: dict) -> dict:
    patient_data = data.get("patient") or {}
    lab_data = data.get("laboratory") or {}

    patient = _build_patient(patient_data)
    lab_org = _build_organization(lab_data.get("name"), lab_data.get("address"))
    ref_practitioner = _build_practitioner(data.get("referring_doctor"))

    observations = [
        _build_lab_observation(obs, _ref(patient), "")
        for obs in (data.get("observations") or [])
        if obs.get("parameter")
    ]

    diag_report: dict[str, Any] = {
        "resourceType": "DiagnosticReport",
        "id": _uid(),
        "meta": _meta("DiagnosticReport"),
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
        "meta": {
            "profile": [f"{_NHCX_BASE}/Bundle"],
            # NHCX claim submission tag
            "tag": [
                {
                    "system": "https://nhcx.abdm.gov.in/fhir/CodeSystem/bundle-use-case",
                    "code": "claim-submission",
                    "display": "NHCX Claim Submission",
                }
            ],
        },
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
