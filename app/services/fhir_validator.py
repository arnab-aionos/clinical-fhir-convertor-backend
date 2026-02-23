"""
FHIR Validator Service
───────────────────────
Lightweight structural validation for FHIR R4 bundles.

Checks mandatory fields per resource type without relying on fhir.resources,
which ships multiple FHIR editions (R4/R4B/R5) across versions and causes
field-name mismatches when the installed version targets a different edition
than our FHIR R4 output.

Also runs NHCX profile compliance checks (warnings, not hard errors).

Returns a ValidationReport with:
  - is_valid: True if no errors
  - errors: list of error strings
  - warnings: list of warning strings
  - resource_count: number of resources in the bundle
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ─── FHIR R4 mandatory fields per resource type ───────────────────────────────
# Only fields that MUST be present for a resource to be structurally valid.
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "Bundle":              ["resourceType", "type"],
    "Patient":             ["resourceType", "id"],
    "Organization":        ["resourceType", "id"],
    "Practitioner":        ["resourceType", "id"],
    "Encounter":           ["resourceType", "id", "status", "class", "subject"],
    "Condition":           ["resourceType", "id", "code", "subject", "clinicalStatus"],
    "Procedure":           ["resourceType", "id", "status", "code", "subject"],
    "Observation":         ["resourceType", "id", "status", "code", "subject"],
    "MedicationStatement": ["resourceType", "id", "status", "subject"],
    "Composition":         ["resourceType", "id", "status", "type", "date", "author", "title"],
    "DiagnosticReport":    ["resourceType", "id", "status", "code", "subject"],
}


def _validate_single_resource(resource: dict) -> list[str]:
    """Check that mandatory FHIR R4 fields are present. Returns list of error strings."""
    resource_type = resource.get("resourceType", "")
    required = _REQUIRED_FIELDS.get(resource_type)
    if required is None:
        return [f"Unknown or unsupported resourceType: {resource_type!r}"]

    return [
        f"{resource_type}: missing required field '{field}'"
        for field in required
        if not resource.get(field)
    ]


def _nhcx_warnings(resource: dict) -> list[str]:
    """Check NHCX-specific requirements and return warnings (not hard errors)."""
    warnings: list[str] = []
    rt = resource.get("resourceType", "")

    # All resources should have meta.profile
    if not resource.get("meta", {}).get("profile"):
        warnings.append(f"{rt}/{resource.get('id', '?')}: missing meta.profile (NHCX compliance)")

    # Patient should have identifier (ABHA ID or UHID)
    if rt == "Patient" and not resource.get("identifier"):
        warnings.append("Patient: missing identifier (ABHA ID or hospital UHID recommended for NHCX)")

    # Observation should have a LOINC code
    if rt == "Observation":
        codings = resource.get("code", {}).get("coding", [])
        has_loinc = any(c.get("system") == "http://loinc.org" for c in codings)
        if not has_loinc:
            param = resource.get("code", {}).get("text", "?")
            warnings.append(f"Observation ({param!r}): no LOINC code – NHCX compliance recommended")

    # Condition should have code
    if rt == "Condition" and not resource.get("code"):
        warnings.append(f"Condition/{resource.get('id', '?')}: missing code")

    return warnings


class ValidationReport:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.resource_count: int = 0

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "resource_count": self.resource_count,
        }


def validate_fhir_bundle(bundle: dict) -> ValidationReport:
    report = ValidationReport()

    if bundle.get("resourceType") != "Bundle":
        report.errors.append("Root resource is not a Bundle")
        return report

    entries = bundle.get("entry", [])
    report.resource_count = len(entries)

    for entry in entries:
        resource = entry.get("resource", {})
        if not resource:
            report.warnings.append("Bundle entry has no 'resource' field")
            continue

        # Structural validation via fhir.resources
        errs = _validate_single_resource(resource)
        report.errors.extend(errs)

        # NHCX compliance warnings
        warns = _nhcx_warnings(resource)
        report.warnings.extend(warns)

    if report.is_valid:
        logger.info("FHIR bundle validation passed. %d resources, %d warnings.",
                    report.resource_count, len(report.warnings))
    else:
        logger.warning("FHIR bundle validation FAILED. %d errors, %d warnings.",
                       len(report.errors), len(report.warnings))

    return report
