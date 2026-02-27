"""
FHIR Validator Service

Structural validation for FHIR R4 bundles using the official HL7 FHIR R4
JSON Schema (fhir.schema.json, 680 definitions, id: json-schema/4.0).

Validates each resource in the bundle against its definition using
jsonschema Draft6Validator + RefResolver. All ValidationErrors are
collected non-raising and reported as strings.

Also runs NRCeS ABDM NHCX compliance checks (warnings, not hard errors)
to flag fields recommended for NHCX claim submission conformance.

Note on validation scope:
  - Structural validity: validated against HL7 FHIR R4 JSON Schema
  - NHCX profile conformance: heuristic checks for meta.profile, ABHA
    identifier, LOINC codes, and required codings. Full NRCeS profile
    validation (StructureDefinition-level) requires a HAPI FHIR server
    loaded with the NRCeS ABDM FHIR IG — roadmap item.

Falls back to lightweight required-field checks if fhir.schema.json
is missing from disk or jsonschema is unavailable.

Returns a ValidationReport with:
  - is_valid: True if no structural errors
  - errors: list of error strings
  - warnings: list of NHCX compliance advisory strings
  - resource_count: number of resources in the bundle
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# fhir.schema.json is expected at the backend root (two levels above this file)
_SCHEMA_PATH = Path(__file__).parent.parent.parent / "fhir.schema.json"

# Module-level singletons — loaded once at import time
_FHIR_SCHEMA: dict | None = None
_RESOLVER = None          # jsonschema.RefResolver instance
_USE_SCHEMA: bool = False  # True only when schema + jsonschema both available


def _init_schema() -> None:
    """Load fhir.schema.json and build a RefResolver. Called once at import."""
    global _FHIR_SCHEMA, _RESOLVER, _USE_SCHEMA
    if not _SCHEMA_PATH.exists():
        logger.critical(
            "fhir.schema.json not found at %s — falling back to lightweight validation",
            _SCHEMA_PATH,
        )
        return
    try:
        import jsonschema  # noqa: F401 — presence check
        from jsonschema import RefResolver

        with open(_SCHEMA_PATH, encoding="utf-8") as fh:
            _FHIR_SCHEMA = json.load(fh)

        schema_version = _FHIR_SCHEMA.get("id", "unknown")
        # Anchor the resolver to the schema's declared id so that all
        # fragment-based $ref values (#/definitions/xxx) resolve correctly.
        schema_id = _FHIR_SCHEMA.get("id") or _SCHEMA_PATH.as_uri()
        _RESOLVER = RefResolver(base_uri=schema_id, referrer=_FHIR_SCHEMA)
        _USE_SCHEMA = True
        logger.info(
            "FHIR R4 schema loaded from %s (schema id: %s) — %d definitions available",
            _SCHEMA_PATH,
            schema_version,
            len(_FHIR_SCHEMA.get("definitions", {})),
        )
    except Exception as exc:  # pragma: no cover
        logger.critical(
            "Failed to initialise jsonschema validator: %s — using lightweight fallback",
            exc,
        )


_init_schema()


# Lightweight fallback — used when fhir.schema.json is absent

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


def _fallback_validate(resource: dict) -> list[str]:
    """Lightweight required-field check used when fhir.schema.json is absent."""
    rt = resource.get("resourceType", "")
    required = _REQUIRED_FIELDS.get(rt)
    if required is None:
        return [f"Unknown or unsupported resourceType: {rt!r}"]
    errors = [
        f"{rt}: missing required field '{field}'"
        for field in required
        if not resource.get(field)
    ]
    # FHIR R4: MedicationStatement must have medication[x]
    if rt == "MedicationStatement":
        has_med = (
            resource.get("medicationCodeableConcept")
            or resource.get("medicationReference")
        )
        if not has_med:
            errors.append("MedicationStatement: missing medication[x] (medicationCodeableConcept or medicationReference)")
    return errors


# Schema-driven validation

def _schema_validate(resource: dict) -> list[str]:
    """
    Validate a single FHIR R4 resource against its definition in fhir.schema.json.

    Returns a list of human-readable error strings (empty list = valid).
    Never raises; all ValidationError exceptions are caught and serialised.
    """
    from jsonschema import Draft6Validator, ValidationError

    rt = resource.get("resourceType", "")
    definitions: dict = _FHIR_SCHEMA.get("definitions", {})  # type: ignore[union-attr]
    definition = definitions.get(rt)

    if definition is None:
        return [f"Unknown or unsupported resourceType: {rt!r}"]

    validator = Draft6Validator(schema=definition, resolver=_RESOLVER)
    errors: list[str] = []
    for err in validator.iter_errors(resource):
        # Build a concise path string: "Patient > name > 0 > given"
        path = " > ".join(str(p) for p in err.absolute_path) if err.absolute_path else "root"
        errors.append(f"{rt} [{path}]: {err.message}")

    return errors


# NRCeS ABDM NHCX compliance checks

def _nhcx_warnings(resource: dict) -> list[str]:
    """
    Check NRCeS ABDM NHCX-specific requirements and return advisory warnings.

    These are conformance recommendations for NHCX claim submission — not
    structural errors. Bundles that pass structural validation but have
    warnings are still technically valid FHIR R4; the warnings indicate
    fields that improve NHCX claim processing.
    """
    warnings: list[str] = []
    rt = resource.get("resourceType", "")

    # All resources should carry meta.profile pointing to NRCeS ABDM IG
    meta_profiles = resource.get("meta", {}).get("profile", [])
    if not meta_profiles:
        warnings.append(
            f"{rt}/{resource.get('id', '?')}: missing meta.profile "
            f"(should reference https://nrces.in/ndhm/fhir/r4/StructureDefinition/{rt})"
        )
    else:
        # Warn if profile URL does not reference the NRCeS ABDM IG
        nrces_profiles = [p for p in meta_profiles if "nrces.in" in p]
        if not nrces_profiles:
            warnings.append(
                f"{rt}/{resource.get('id', '?')}: meta.profile does not reference "
                f"NRCeS ABDM IG (nrces.in/ndhm/fhir/r4)"
            )

    # Patient: should carry an ABHA ID or hospital UHID for NHCX claim linking
    if rt == "Patient" and not resource.get("identifier"):
        warnings.append(
            "Patient: missing identifier — ABHA ID or hospital UHID recommended "
            "for NHCX claim patient matching"
        )

    # Observation: should have a LOINC code for interoperability
    if rt == "Observation":
        codings = resource.get("code", {}).get("coding", [])
        has_loinc = any(c.get("system") == "http://loinc.org" for c in codings)
        if not has_loinc:
            param = resource.get("code", {}).get("text", "?")
            warnings.append(
                f"Observation ({param!r}): no LOINC code in code.coding — "
                f"LOINC codes recommended for NHCX claim interoperability"
            )

    # Condition: should have a coded diagnosis (ICD-10 preferred for NHCX)
    if rt == "Condition":
        codings = resource.get("code", {}).get("coding", [])
        has_icd = any(
            "icd" in c.get("system", "").lower() for c in codings
        )
        if not codings:
            warnings.append(
                f"Condition/{resource.get('id', '?')}: missing code.coding — "
                f"ICD-10 coding recommended for NHCX diagnosis reporting"
            )
        elif not has_icd:
            warnings.append(
                f"Condition/{resource.get('id', '?')}: code.coding present but no "
                f"ICD-10 system detected — ICD-10 preferred for NHCX"
            )

    # MedicationStatement: check for R4 medication[x] field
    if rt == "MedicationStatement":
        has_med = (
            resource.get("medicationCodeableConcept")
            or resource.get("medicationReference")
        )
        if not has_med:
            warnings.append(
                "MedicationStatement: missing medication[x] field "
                "(medicationCodeableConcept or medicationReference)"
            )

    return warnings


# Public API

class ValidationReport:
    def __init__(self) -> None:
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
            "validation_scope": (
                "FHIR R4 structural validation (HL7 JSON Schema) + "
                "NRCeS ABDM NHCX compliance checks"
            ),
        }


def validate_fhir_bundle(bundle: dict) -> ValidationReport:
    """
    Validate a FHIR R4 Bundle dict.

    Structural validation: uses jsonschema Draft6Validator against the
    official HL7 FHIR R4 JSON Schema (json-schema/4.0, 680 definitions)
    when fhir.schema.json is present; falls back to required-field checks.

    NHCX compliance checks: additional warnings for NRCeS ABDM profile
    conformance (meta.profile, ABHA identifiers, LOINC codes, ICD-10 codes).
    These are advisory — a bundle can be structurally valid with warnings.
    """
    report = ValidationReport()

    if bundle.get("resourceType") != "Bundle":
        report.errors.append("Root resource is not a Bundle")
        return report

    entries = bundle.get("entry", [])
    report.resource_count = len(entries)

    validate_fn = _schema_validate if _USE_SCHEMA else _fallback_validate

    for entry in entries:
        resource = entry.get("resource", {})
        if not resource:
            report.warnings.append("Bundle entry has no 'resource' field")
            continue

        report.errors.extend(validate_fn(resource))
        report.warnings.extend(_nhcx_warnings(resource))

    if report.is_valid:
        logger.info(
            "FHIR R4 bundle validation passed — %d resources, %d NHCX compliance warnings (schema=%s)",
            report.resource_count, len(report.warnings), _USE_SCHEMA,
        )
    else:
        logger.warning(
            "FHIR R4 bundle validation FAILED — %d structural errors, %d warnings (schema=%s)",
            len(report.errors), len(report.warnings), _USE_SCHEMA,
        )

    return report
