"""
Medical Abbreviation Expander

Expands Indian clinical document abbreviations before LLM extraction.
Significantly improves extraction accuracy — the LLM handles
"history of diabetes mellitus" much better than "H/O DM".

Coverage: abbreviations from the 42 diagnostic report + 72 discharge summary
samples analyzed from the NHCX dataset.
"""

import re
import logging

logger = logging.getLogger(__name__)

# Master abbreviation table.
# Sorted longest-first so longer keys (K/C/O) match before shorter overlapping ones (C/O).

ABBREVIATIONS: dict[str, str] = {
    # History / Complaints
    "C/O":        "complaining of",
    "H/O":        "history of",
    "K/C/O":      "known case of",
    "N/K/C/O":    "not known case of",
    "N/K/C/OD":   "not known case of diabetes",
    "K/C/OD":     "known case of diabetes",

    # Examination
    "S/E":        "systemic examination",
    "G/E":        "general examination",
    "L/E":        "local examination",
    "P/E":        "physical examination",
    "P/A":        "per abdomen",
    "P/R":        "per rectum",
    "P/V":        "per vaginum",

    # Systems
    "CVS":        "cardiovascular system",
    "RS":         "respiratory system",
    "CNS":        "central nervous system",
    "GIT":        "gastrointestinal tract",
    "GU":         "genitourinary",
    "MSS":        "musculoskeletal system",

    # Clinical findings (negative/normal)
    "NFND":       "no focal neurological deficit",
    "NVBS":       "normal vesicular breath sounds",
    "NAD":        "no abnormality detected",
    "WNL":        "within normal limits",
    "NL":         "normal",
    "NFD":        "no focal deficit",
    "NEAD":       "no evidence of active disease",

    # Medications / Frequency
    "OD":         "once daily",
    "BD":         "twice daily",
    "TDS":        "three times daily",
    "QID":        "four times daily",
    "SOS":        "as needed",
    "PRN":        "as needed",
    "HS":         "at bedtime",
    "AC":         "before meals",
    "PC":         "after meals",
    "M/A/N":      "morning / afternoon / night",
    "M/A/N/B":    "morning / afternoon / night / bedtime",
    "M/A/N/B/F":  "morning / afternoon / night / bedtime / frequency",
    "F/A":        "frequency advice",
    "IV":         "intravenous",
    "IM":         "intramuscular",
    "SC":         "subcutaneous",
    "PO":         "orally",
    "S/C":        "subcutaneous",
    "I/V":        "intravenous",
    "I/M":        "intramuscular",

    # Diagnoses / Conditions
    "DM":         "diabetes mellitus",
    "HTN":        "hypertension",
    "CAD":        "coronary artery disease",
    "ACS":        "acute coronary syndrome",
    "MI":         "myocardial infarction",
    "STEMI":      "ST elevation myocardial infarction",
    "NSTEMI":     "non-ST elevation myocardial infarction",
    "CCF":        "congestive cardiac failure",
    "CHF":        "congestive heart failure",
    "AF":         "atrial fibrillation",
    "LVF":        "left ventricular failure",
    "RVF":        "right ventricular failure",
    "IHD":        "ischemic heart disease",
    "DVT":        "deep vein thrombosis",
    "PE":         "pulmonary embolism",
    "CKD":        "chronic kidney disease",
    "AKI":        "acute kidney injury",
    "UTI":        "urinary tract infection",
    "URTI":       "upper respiratory tract infection",
    "LRTI":       "lower respiratory tract infection",
    "COPD":       "chronic obstructive pulmonary disease",
    "TB":         "tuberculosis",
    "PTB":        "pulmonary tuberculosis",
    "CVA":        "cerebrovascular accident",
    "TIA":        "transient ischemic attack",
    "GDM":        "gestational diabetes mellitus",
    "PIH":        "pregnancy induced hypertension",
    "APH":        "antepartum hemorrhage",
    "PPH":        "postpartum hemorrhage",
    "LSCS":       "lower segment cesarean section",

    # Vitals
    "BP":         "blood pressure",
    "HR":         "heart rate",
    "PR":         "pulse rate",
    "RR":         "respiratory rate",
    "SpO2":       "oxygen saturation",
    "Temp":       "temperature",
    "Wt":         "weight",
    "Ht":         "height",
    "BMI":        "body mass index",

    # Laboratory / Investigations
    "CBC":        "complete blood count",
    "CBP":        "complete blood picture",
    "Hb":         "hemoglobin",
    "HB":         "hemoglobin",
    "PCV":        "packed cell volume",
    "TLC":        "total leukocyte count",
    "DLC":        "differential leukocyte count",
    "PLT":        "platelets",
    "RBC":        "red blood cells",
    "WBC":        "white blood cells",
    "MCV":        "mean corpuscular volume",
    "MCH":        "mean corpuscular hemoglobin",
    "MCHC":       "mean corpuscular hemoglobin concentration",
    "ESR":        "erythrocyte sedimentation rate",
    "CRP":        "C-reactive protein",
    "LFT":        "liver function test",
    "RFT":        "renal function test",
    "KFT":        "kidney function test",
    "TFT":        "thyroid function test",
    "FBS":        "fasting blood sugar",
    "RBS":        "random blood sugar",
    "PPBS":       "postprandial blood sugar",
    "HbA1c":      "glycated hemoglobin",
    "BUN":        "blood urea nitrogen",
    "S/Cr":       "serum creatinine",
    "S/Na":       "serum sodium",
    "S/K":        "serum potassium",
    "S/Cl":       "serum chloride",
    "S. Creatinine": "serum creatinine",
    "S. Sodium":  "serum sodium",
    "PT":         "prothrombin time",
    "INR":        "international normalized ratio",
    "APTT":       "activated partial thromboplastin time",
    "ECG":        "electrocardiogram",
    "ECHO":       "echocardiogram",
    "USG":        "ultrasonography",
    "CT":         "computed tomography",
    "MRI":        "magnetic resonance imaging",
    "CXR":        "chest X-ray",
    "2D ECHO":    "2-dimensional echocardiogram",
    "EF":         "ejection fraction",
    "LVEF":       "left ventricular ejection fraction",

    # Procedure / Surgical
    "CABG":       "coronary artery bypass grafting",
    "PTCA":       "percutaneous transluminal coronary angioplasty",
    "PCI":        "percutaneous coronary intervention",
    "ERCP":       "endoscopic retrograde cholangiopancreatography",
    "OT":         "operation theatre",
    "GA":         "general anaesthesia",
    "LA":         "local anaesthesia",
    "SA":         "spinal anaesthesia",

    # Discharge / Miscellaneous
    "DAMA":       "discharged against medical advice",
    "D/D":        "differential diagnosis",
    "A/W":        "associated with",
    "B/L":        "bilateral",
    "U/L":        "unilateral",
    "R/O":        "rule out",
    "c/o":        "complaining of",
    "h/o":        "history of",
    "k/c/o":      "known case of",
}

# Compiled regex — sorted by length so longer keys match before shorter overlapping ones.
_SORTED_ABBREVS = sorted(ABBREVIATIONS.keys(), key=len, reverse=True)

# Escape and join into one pattern. Use word boundaries where possible,
# but allow slash-containing abbreviations to match as tokens.
_PATTERN = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(a) for a in _SORTED_ABBREVS) + r")(?!\w)",
    re.IGNORECASE,
)


def expand(text: str) -> str:
    """
    Expand medical abbreviations in `text`.
    Returns expanded text. Preserves case of surrounding content.

    Example:
        "Pt C/O chest pain H/O DM." →
        "Pt complaining of chest pain history of diabetes mellitus."
    """
    def _replace(match: re.Match) -> str:
        token = match.group(1)
        # Lookup with original case first, then upper-cased key
        expansion = ABBREVIATIONS.get(token) or ABBREVIATIONS.get(token.upper())
        if expansion:
            return expansion
        return token  # No match found – leave as-is

    expanded = _PATTERN.sub(_replace, text)
    if expanded != text:
        logger.debug("Abbreviation expansion applied (%d chars → %d chars).", len(text), len(expanded))
    return expanded


def get_abbreviation_hint() -> str:
    """Return a compact abbreviation reference for inclusion in LLM system prompts."""
    lines = ["Common Indian medical abbreviations in this document (already expanded):"]
    sample = list(ABBREVIATIONS.items())[:20]  # Show a sample in the prompt
    for abbr, full in sample:
        lines.append(f"  {abbr} = {full}")
    lines.append("  … and others. Treat expanded forms as standard English clinical terms.")
    return "\n".join(lines)
