from typing import Optional
from pydantic import BaseModel, Field, field_validator


def _to_str(v):
    """Coerce numeric (or any non-None) LLM values to str for Optional[str] fields."""
    return str(v) if v is not None else None


# ─── Shared ───────────────────────────────────────────────────────────────────

class PatientInfo(BaseModel):
    name: Optional[str] = None
    age: Optional[str] = None
    gender: Optional[str] = None
    id: Optional[str] = None          # UHID / MRD / patient ID from document
    address: Optional[str] = None
    dob: Optional[str] = None

    @field_validator('age', 'id', mode='before')
    @classmethod
    def coerce_to_str(cls, v):
        return _to_str(v)


# ─── Discharge Summary ─────────────────────────────────────────────────────────

class EncounterInfo(BaseModel):
    admission_date: Optional[str] = None
    discharge_date: Optional[str] = None
    department: Optional[str] = None
    ward: Optional[str] = None
    bed_number: Optional[str] = None
    hospital_name: Optional[str] = None
    hospital_address: Optional[str] = None


class DiagnosisInfo(BaseModel):
    text: Optional[str] = None
    type: Optional[str] = "final"     # "provisional" | "final"
    icd_code: Optional[str] = None


class ProcedureInfo(BaseModel):
    text: Optional[str] = None
    date: Optional[str] = None


class VitalsInfo(BaseModel):
    bp: Optional[str] = None           # Blood Pressure e.g. "120/80 mmHg"
    pulse: Optional[str] = None        # e.g. "72 bpm"
    temp: Optional[str] = None         # e.g. "98.6 F"
    spo2: Optional[str] = None         # e.g. "98%"
    rr: Optional[str] = None           # Respiratory Rate e.g. "18/min"
    weight: Optional[str] = None
    height: Optional[str] = None


class InvestigationResult(BaseModel):
    test: Optional[str] = None
    result: Optional[str] = None
    unit: Optional[str] = None
    ref_range: Optional[str] = None
    is_abnormal: Optional[bool] = None

    @field_validator('result', 'unit', mode='before')
    @classmethod
    def coerce_to_str(cls, v):
        return _to_str(v)


class MedicationInfo(BaseModel):
    drug: Optional[str] = None
    dosage: Optional[str] = None
    frequency: Optional[str] = None   # e.g. "OD", "BD", "TDS", "M-A-N-B"
    duration: Optional[str] = None
    route: Optional[str] = None       # e.g. "Oral", "IV"


class DischargeSummaryData(BaseModel):
    document_type: str = "discharge_summary"
    patient: PatientInfo = Field(default_factory=PatientInfo)
    encounter: EncounterInfo = Field(default_factory=EncounterInfo)
    treating_doctor: Optional[str] = None
    diagnoses: list[DiagnosisInfo] = Field(default_factory=list)
    procedures: list[ProcedureInfo] = Field(default_factory=list)
    chief_complaints: list[str] = Field(default_factory=list)
    history_of_present_illness: Optional[str] = None
    past_history: Optional[str] = None
    vitals: VitalsInfo = Field(default_factory=VitalsInfo)
    investigations: list[InvestigationResult] = Field(default_factory=list)
    medications: list[MedicationInfo] = Field(default_factory=list)
    condition_at_discharge: Optional[str] = None
    follow_up: Optional[str] = None
    course_in_hospital: Optional[str] = None


# ─── Diagnostic Report ─────────────────────────────────────────────────────────

class LaboratoryInfo(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None


class ObservationResult(BaseModel):
    parameter: Optional[str] = None
    result: Optional[str] = None
    unit: Optional[str] = None
    reference_range: Optional[str] = None
    is_abnormal: Optional[bool] = False
    loinc_code: Optional[str] = None   # LLM-assisted mapping

    @field_validator('result', 'unit', mode='before')
    @classmethod
    def coerce_to_str(cls, v):
        return _to_str(v)


class DiagnosticReportData(BaseModel):
    document_type: str = "diagnostic_report"
    patient: PatientInfo = Field(default_factory=PatientInfo)
    laboratory: LaboratoryInfo = Field(default_factory=LaboratoryInfo)
    report_date: Optional[str] = None
    sample_date: Optional[str] = None
    referring_doctor: Optional[str] = None
    test_category: Optional[str] = None   # "haematology|biochemistry|serology|cardiology"
    observations: list[ObservationResult] = Field(default_factory=list)
    interpretation: Optional[str] = None
    comments: Optional[str] = None
