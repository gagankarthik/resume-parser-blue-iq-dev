"""Resume domain models — the OpenAI structured-output target.

Validators SANITIZE rather than raise, so malformed LLM output never crashes the
pipeline. These are the models the parser produces and the API returns.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.schemas.validators import (
    _coerce_list,
    _sanitize_date,
    _sanitize_email,
    _sanitize_phone,
    _sanitize_str,
    _sanitize_url,
    _sanitize_year,
    _sanitize_yes_no_na,
)


class SpecialtyMatch(BaseModel):
    """A single clinical specialty for a role, mapped to the specialty catalog.

    The LLM only supplies `name` (the unit/specialty text it read off the résumé).
    Everything else is filled deterministically in post-processing by the specialty
    matcher (`app/services/normalization/specialty_matcher.py`): it cleans the name,
    looks it up against the catalog through a tiered match, and stamps the
    `specialty_id`, `group`, `confidence`, `matched` flag, and which `match_tier`
    fired. Values the model emits for those fields are IGNORED — the matcher always
    overwrites them — so the model can never inject a bogus id or confidence.

    A specialty that does not map to the catalog is kept with `specialty_id=None`
    and `matched=False` (never dropped) so it can be surfaced for admin review.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Med Surg / Tele",
                "raw": "Med Surg/Tele",
                "specialty_id": "1042",
                "group": "Med Surg / Tele",
                "confidence": 1.0,
                "matched": True,
                "match_tier": "name",
            }
        }
    )

    name:         str        = Field(..., description="The unit/specialty named for this role (e.g. 'Med Surg/Tele', 'ICU'). This is the ONLY field you fill — leave everything else null/default; the system maps it to a specialty id and confidence.")
    raw:          str | None = Field(None, description="Original specialty text as written, preserved for review. Set by the system.")
    specialty_id: str | None = Field(None, description="Matched specialty catalog id, or null when unmatched. Set by the system — never fill this yourself.")
    group:        str | None = Field(None, description="Specialty group label. Set by the system.")
    confidence:   float      = Field(0.0, ge=0.0, le=1.0, description="Match confidence 0.0–1.0. Set by the system — never fill this yourself.")
    matched:      bool       = Field(False, description="True only when a specialty_id was assigned. Set by the system.")
    match_tier:   str | None = Field(None, description="Which match tier resolved the id ('name' | 'full_name' | 'keywords' | 'ai'), or null when unmatched. Set by the system.")

    @field_validator("name", mode="before")
    @classmethod
    def _required_name(cls, v: object) -> str:
        return v.strip() if isinstance(v, str) and v.strip() else "Unknown"

    @field_validator("raw", "group", "match_tier", mode="before")
    @classmethod
    def _sanitize_optional(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("specialty_id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> str | None:
        # Accept ints or strings from the catalog without guessing the platform's
        # id type — store everything as a string. Blank/None → None.
        if v is None:
            return None
        if isinstance(v, bool):  # guard: bool is an int subclass
            return None
        if isinstance(v, int | float):
            return str(int(v)) if float(v).is_integer() else str(v)
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: object) -> float:
        if isinstance(v, bool) or not isinstance(v, int | float | str):
            return 0.0
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return min(max(f, 0.0), 1.0)

    @field_validator("matched", mode="before")
    @classmethod
    def _coerce_matched(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in {"yes", "y", "true"}
        return False


class PersonalInfo(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "full_name": "Jane Smith",
                "credentials": ["RN", "BSN", "CCRN"],
                "email": "jane.smith@email.com",
                "phone": "+1 555 234 5678",
                "location": "Houston, TX",
                "linkedin_url": "https://linkedin.com/in/janesmith",
                "github_url": None,
                "portfolio_url": None,
                "summary": "ICU Registered Nurse with 8 years of critical care experience.",
            }
        }
    )

    full_name:      str | None = Field(None, description="Candidate's name ONLY — exclude trailing credential/licence/degree suffixes (e.g. 'Jane Smith', not 'Jane Smith, RN BSN')")
    credentials:    list[str]    = Field(default_factory=list, description="Post-nominal credentials that follow the candidate's name (e.g. 'RN', 'BSN', 'MPH', 'CCRN'), each as a separate item, in the order written. These are stripped from full_name and MUST be preserved here — never drop them.")
    email:          str | None = Field(None, description="Primary email address")
    phone:          str | None = Field(None, description="Primary phone number in original format")
    location:       str | None = Field(None, description="The candidate's FULL address line exactly as written, including street/number if present (e.g. '135 Brush Hill Road, Milton, MA 02186'). Do NOT shorten it to just city/state/zip.")
    linkedin_url:   str | None = Field(None, description="LinkedIn profile URL")
    github_url:     str | None = Field(None, description="GitHub profile URL")
    portfolio_url:  str | None = Field(None, description="Personal website or portfolio URL")
    summary:        str | None = Field(None, description="Professional summary or objective statement")

    @field_validator("full_name", "location", "summary", mode="before")
    @classmethod
    def sanitize_strings(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("credentials", mode="before")
    @classmethod
    def coerce_credentials(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [s for i in items if isinstance(i, str) and (s := i.strip())]

    @field_validator("email", mode="before")
    @classmethod
    def sanitize_email(cls, v: object) -> str | None:
        return _sanitize_email(str(v)) if isinstance(v, str) else None

    @field_validator("linkedin_url", "github_url", "portfolio_url", mode="before")
    @classmethod
    def sanitize_urls(cls, v: object) -> str | None:
        return _sanitize_url(str(v)) if isinstance(v, str) else None

    @field_validator("phone", mode="before")
    @classmethod
    def sanitize_phone(cls, v: object) -> str | None:
        return _sanitize_phone(str(v)) if isinstance(v, str) else None


class ExperienceItem(BaseModel):
    """
    A single work-history entry.

    Field names mirror the platform's "Edit Work History" form so a parsed entry
    maps straight onto it: `company` → Facility Name, `is_current` → Currently
    Employed, `city`/`state`/`country`/`zip_code` → the location block,
    `profession`/`specialties` → Select Profession / Select Specialties, and the
    facility/position attributes feed the optional "Additional Details" section.
    All of these extra fields are optional and stay null/empty when the resume
    doesn't state them. (Latitude/longitude are intentionally omitted — the
    platform geocodes them from city/state/zip.)
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "company": "Fort Sanders Regional Medical Center",
                "role": "RN - Med Surg/Tele",
                "start_date": "01/01/2026",
                "end_date": "04/30/2026",
                "is_current": False,
                "location": "1901 W Clinch Ave, Knoxville, TN 37916",
                "city": "Knoxville",
                "state": "TN",
                "country": None,
                "zip_code": "37916",
                "employer_phone": "865-541-1111",
                "profession": "RN",
                "specialties": [{"name": "Med Surg / Tele"}],
                "service_type": None,
                "nurse_to_patient_ratio": "1:5",
                "facility_beds": None,
                "beds_in_unit": "30",
                "teaching_facility": "N/A",
                "magnet_facility": "N/A",
                "trauma_facility": "N/A",
                "trauma_level": None,
                "additional_info": None,
                "position_held": "Staff Nurse",
                "agency_name": None,
                "charge_experience": None,
                "charting_system": "Epic",
                "shift": "Nights",
                "reason_for_leaving": None,
                "description": [
                    "Provided care on a 30-bed Med Surg/Telemetry unit.",
                    "Managed post-operative and cardiac step-down patients.",
                ],
                "achievements": [
                    "Charge nurse for 18-month period",
                    "Preceptor for 6 new graduate nurses",
                ],
            }
        }
    )

    company:       str           = Field(..., description="Employer or facility name (maps to 'Facility Name')")
    role:          str           = Field(..., description="Job title or role, including credential if present (e.g. 'RN - MICU')")
    start_date:    str | None = Field(None, description="Start date as MM/DD/YYYY. If only month/year is stated use MM/YYYY; if only a year, YYYY. NEVER invent a missing day or month.")
    end_date:      str | None = Field(None, description="End date as MM/DD/YYYY (or MM/YYYY / YYYY when the day/month is not stated), or 'Present' for a current role.")
    is_current:    bool          = Field(False, description="True if this is the candidate's current position ('Currently Employed')")
    location:      str | None = Field(None, description="The FULL workplace location/address exactly as written on the résumé, including street if present (e.g. '500 J Clyde Morris Blvd, Newport News, VA 23601')")
    # ── Structured facility location (Work History form) ──────────────────────
    city:          str | None = Field(None, description="Facility city, only if stated — copied verbatim")
    state:         str | None = Field(None, description="Facility state/province, only if stated — copied exactly as written (keep 'NY', do NOT expand to 'New York')")
    country:       str | None = Field(None, description="Facility country — ONLY if explicitly written on the résumé; otherwise null. Do NOT infer 'United States'.")
    zip_code:      str | None = Field(None, description="Facility postal/ZIP code, only if stated — never guessed from the city")
    employer_phone: str | None = Field(None, description="Employer/facility phone number exactly as written, only if stated next to this role (e.g. '304-287-2120'). Null if not stated.")
    # ── Clinical classification (Select Profession / Select Specialties) ──────
    profession:    str | None = Field(None, description="Credential/profession for this role exactly as stated (e.g. 'RN', 'LPN', 'CRT'). Do NOT expand the abbreviation.")
    specialties:   list[SpecialtyMatch] = Field(default_factory=list, description="Clinical specialties/units for this role, one object per specialty. Fill ONLY the `name` field of each (e.g. {\"name\": \"Med Surg/Tele\"}, {\"name\": \"ICU\"}); the system fills the id/confidence/group.")
    # ── Facility attributes (Additional Details — only if stated) ─────────────
    service_type:           str | None = Field(None, description="Service type, if stated")
    nurse_to_patient_ratio: str | None = Field(None, description="Nurse-to-patient ratio, if stated (e.g. '1:5')")
    facility_beds:          str | None = Field(None, description="Total facility bed count, if stated")
    beds_in_unit:           str | None = Field(None, description="Bed count for the unit, if stated")
    teaching_facility:      str | None = Field(None, description="'Yes', 'No', or 'N/A' — only if stated, else null")
    magnet_facility:        str | None = Field(None, description="'Yes', 'No', or 'N/A' — only if stated, else null")
    trauma_facility:        str | None = Field(None, description="'Yes', 'No', or 'N/A' — only if stated, else null")
    trauma_level:           str | None = Field(None, description="Trauma level (e.g. 'Level I'), if stated")
    additional_info:        str | None = Field(None, description="Any other facility detail stated for this role")
    # ── Position details (Work History form) ──────────────────────────────────
    position_held:          str | None = Field(None, description="Position/title held, if distinct from the credential (e.g. 'Staff Nurse', 'Charge Nurse')")
    agency_name:            str | None = Field(None, description="Staffing/travel agency name, if this was an agency assignment")
    charge_experience:      str | None = Field(None, description="Charge experience, if stated")
    charting_system:        str | None = Field(None, description="EHR/charting system used (e.g. 'Epic', 'Cerner', 'Meditech'), if stated")
    shift:                  str | None = Field(None, description="Shift worked (e.g. 'Days', 'Nights', 'Rotating'), if stated")
    reason_for_leaving:     str | None = Field(None, description="Reason for leaving, if stated")
    description:   list[str]     = Field(default_factory=list, description="Role responsibilities as an array of short strings — one item per responsibility/bullet, never a single paragraph")
    achievements:  list[str]     = Field(default_factory=list, description="Notable accomplishments in this role")

    @field_validator("company", "role", mode="before")
    @classmethod
    def required_strings(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            return "Unknown"
        return v.strip()

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def sanitize_dates(cls, v: object) -> str | None:
        return _sanitize_date(str(v)) if isinstance(v, str) else None

    @field_validator(
        "location", "city", "state", "country", "zip_code", "profession",
        "service_type", "nurse_to_patient_ratio", "facility_beds", "beds_in_unit",
        "trauma_level", "additional_info", "position_held", "agency_name",
        "charge_experience", "charting_system", "shift", "reason_for_leaving",
        mode="before",
    )
    @classmethod
    def sanitize_optional_strings(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("employer_phone", mode="before")
    @classmethod
    def sanitize_employer_phone(cls, v: object) -> str | None:
        return _sanitize_phone(str(v)) if isinstance(v, str) else None

    @field_validator("teaching_facility", "magnet_facility", "trauma_facility", mode="before")
    @classmethod
    def sanitize_yes_no_na(cls, v: object) -> str | None:
        return _sanitize_yes_no_na(str(v)) if isinstance(v, str) else None

    @field_validator("specialties", mode="before")
    @classmethod
    def coerce_specialties(cls, v: object) -> list[dict]:
        # Accept a bare string (LLM shorthand / legacy callers), a dict (the model's
        # structured object, or a DynamoDB reload), or a SpecialtyMatch. Each becomes
        # SpecialtyMatch input carrying just the name — the id/confidence/group are
        # filled later by the specialty matcher, never here.
        out: list[dict] = []
        for i in _coerce_list(v):
            if isinstance(i, str):
                if s := i.strip():
                    out.append({"name": s})
            elif isinstance(i, SpecialtyMatch):
                out.append(i.model_dump())
            elif isinstance(i, dict):
                name = i.get("name")
                if isinstance(name, str) and name.strip():
                    out.append(i)
        return out

    @field_validator("description", mode="before")
    @classmethod
    def coerce_description(cls, v: object) -> list[str]:
        # Always a list of strings — one item per bullet/line. When the model
        # emits the whole block as a single string, split on line breaks and
        # bullet glyphs only — NOT on sentence boundaries — so a multi-sentence
        # bullet ("Works in the CCU. Also worked in the SICU.") stays one item.
        if isinstance(v, str):
            # Split on line breaks and true bullet glyphs only. The middle dot '·'
            # is excluded — it is commonly an inline separator ("Charge nurse · ICU").
            parts = re.split(r"[\r\n]+|\s*[•▪◦‣]\s*", v.strip())
            cleaned = (re.sub(r"^[-*]\s+", "", p) for p in parts)
        else:
            cleaned = (i for i in _coerce_list(v) if isinstance(i, str))
        # Collapse intra-bullet whitespace so a PDF line-wrap inside one bullet
        # ("ensuring\naccurate documentation") becomes a single space, not a
        # literal newline left in the string.
        return [s for c in cleaned if (s := re.sub(r"\s+", " ", c).strip())]

    @field_validator("achievements", mode="before")
    @classmethod
    def coerce_achievements(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [i for i in items if isinstance(i, str) and i.strip()]

    @model_validator(mode="after")
    def sync_is_current(self) -> ExperienceItem:
        if self.end_date and self.end_date.lower() == "present":
            self.is_current = True
        return self


class EducationItem(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "institution": "University of Texas Health Science Center",
                "degree": "Bachelor of Science in Nursing",
                "field_of_study": "Nursing",
                "start_year": 2012,
                "graduation_year": 2016,
                "gpa": "3.8",
            }
        }
    )

    institution:      str           = Field(..., description="Name of the school or university")
    degree:           str | None = Field(None, description="Degree earned (e.g. 'Bachelor of Science in Nursing')")
    field_of_study:   str | None = Field(None, description="Major or field of study")
    start_year:       int | None = Field(None, description="Year started (1900–2035)", ge=1900, le=2035)
    graduation_year:  int | None = Field(None, description="Graduation year (1900–2035)", ge=1900, le=2035)
    gpa:              str | None = Field(None, description="GPA if stated")

    @field_validator("institution", mode="before")
    @classmethod
    def required_institution(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            return "Unknown Institution"
        return v.strip()

    @field_validator("degree", "field_of_study", "gpa", mode="before")
    @classmethod
    def sanitize_optional(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("start_year", "graduation_year", mode="before")
    @classmethod
    def sanitize_year(cls, v: object) -> int | None:
        if isinstance(v, int | float):
            return _sanitize_year(int(v))
        if isinstance(v, str) and v.strip().isdigit():
            return _sanitize_year(int(v.strip()))
        return None


class CertificationItem(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "BLS",
                "issuer": "American Heart Association",
                "issued_date": None,
                "expiry_date": None,
                "date": "12/2024",
                "credential_id": None,
            }
        }
    )

    name:           str           = Field(..., description="Certification name (e.g. 'ACLS', 'BLS', 'CCRN')")
    issuer:         str | None = Field(None, description="Issuing organisation")
    issued_date:    str | None = Field(None, description="Issue/award date — ONLY when the résumé labels it as issued/awarded/completed. MM/DD/YYYY, or MM/YYYY / YYYY. Never invent parts.")
    expiry_date:    str | None = Field(None, description="Expiry/renewal date — ONLY when the résumé labels it as expires/valid through/renewal. MM/DD/YYYY, or MM/YYYY / YYYY. Never invent parts.")
    date:           str | None = Field(None, description="A certification date when the résumé does NOT say whether it is the issue or the expiry date (e.g. 'BLS: 12/2024'). Do NOT assume expiry — put unlabeled dates here. Same MM/DD/YYYY / MM/YYYY / YYYY format.")
    credential_id:  str | None = Field(None, description="Credential ID or certificate number")

    @field_validator("name", mode="before")
    @classmethod
    def required_name(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            return "Unknown Certification"
        return v.strip()

    @field_validator("issuer", "credential_id", mode="before")
    @classmethod
    def sanitize_optional(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("issued_date", "expiry_date", "date", mode="before")
    @classmethod
    def sanitize_dates(cls, v: object) -> str | None:
        return _sanitize_date(str(v)) if isinstance(v, str) else None


class LicenseItem(BaseModel):
    """
    A professional license — distinct from a certification.

    State nursing/allied-health licenses ("Active FL RN License #RN9411204",
    "New York State Registered Nurse License", compact/multistate licenses) are a
    licence, not a certification (BLS/ACLS/CCRN…). They are captured here so a
    state licence number is never lost or mislabeled as a cert.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Registered Nurse License",
                "license_type": "RN",
                "state": "FL",
                "license_number": "RN9411204",
                "issued_date": None,
                "expiry_date": None,
                "is_compact": False,
                "status": "Active",
            }
        }
    )

    name:           str           = Field(..., description="License name as written (e.g. 'Registered Nurse License', 'Radiologic Technologist License')")
    license_type:   str | None = Field(None, description="Credential/profession the licence is for, as written (e.g. 'RN', 'LPN', 'RT'); do NOT expand")
    state:          str | None = Field(None, description="Issuing US state/territory, as written (keep 'NY' — do NOT expand to 'New York'). Null if not stated.")
    license_number: str | None = Field(None, description="License/permit number exactly as written, including any letter prefix (e.g. 'RN9411204'). Null if not stated.")
    issued_date:    str | None = Field(None, description="Issue date — only if stated. MM/DD/YYYY, or MM/YYYY / YYYY. Never invent parts.")
    expiry_date:    str | None = Field(None, description="Expiry/renewal date — only if stated. MM/DD/YYYY, or MM/YYYY / YYYY. Never invent parts.")
    is_compact:     bool          = Field(False, description="True only if the résumé explicitly calls it a compact/multistate/eNLC licence")
    status:         str | None = Field(None, description="Licence status if stated (e.g. 'Active', 'Inactive', 'In progress')")

    @field_validator("name", mode="before")
    @classmethod
    def required_name(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            return "Unknown License"
        return v.strip()

    @field_validator("license_type", "state", "license_number", "status", mode="before")
    @classmethod
    def sanitize_optional(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("issued_date", "expiry_date", mode="before")
    @classmethod
    def sanitize_dates(cls, v: object) -> str | None:
        return _sanitize_date(str(v)) if isinstance(v, str) else None

    @field_validator("is_compact", mode="before")
    @classmethod
    def coerce_bool(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in {"yes", "y", "true", "compact", "multistate"}
        return False


class ProjectItem(BaseModel):
    name:          str           = Field(..., description="Project name")
    description:   str | None = Field(None, description="Brief description of the project")
    technologies:  list[str]     = Field(default_factory=list, description="Technologies used")
    url:           str | None = Field(None, description="Project URL")

    @field_validator("name", mode="before")
    @classmethod
    def required_name(cls, v: object) -> str:
        return str(v).strip() if isinstance(v, str) and str(v).strip() else "Untitled Project"

    @field_validator("technologies", mode="before")
    @classmethod
    def coerce_list(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [i for i in items if isinstance(i, str) and i.strip()]


class ReferenceItem(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Dr. Maria Alvarez",
                "relationship": "ICU Nurse Manager",
                "company": "Memorial Hermann Hospital",
                "email": "m.alvarez@example.com",
                "phone": "+1 555 010 2233",
            }
        }
    )

    name:          str        = Field(..., description="Reference's full name")
    relationship:  str | None = Field(None, description="Relationship or title (e.g. 'Charge Nurse', 'Former Manager')")
    company:       str | None = Field(None, description="Organisation where you worked together")
    email:         str | None = Field(None, description="Reference email address")
    phone:         str | None = Field(None, description="Reference phone number")

    @field_validator("name", mode="before")
    @classmethod
    def required_name(cls, v: object) -> str:
        return str(v).strip() if isinstance(v, str) and str(v).strip() else "Unknown Reference"

    @field_validator("relationship", "company", mode="before")
    @classmethod
    def sanitize_optional(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("email", mode="before")
    @classmethod
    def sanitize_email(cls, v: object) -> str | None:
        return _sanitize_email(str(v)) if isinstance(v, str) else None

    @field_validator("phone", mode="before")
    @classmethod
    def sanitize_phone(cls, v: object) -> str | None:
        return _sanitize_phone(str(v)) if isinstance(v, str) else None


class ParsedResumeAI(BaseModel):
    """
    Structured output schema enforced on the OpenAI response.
    All list fields default to [] so the model is never None on missing sections.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "personal_info": {
                    "full_name": "Jane Smith",
                    "email": "jane.smith@email.com",
                    "phone": "+1 555 234 5678",
                    "location": "Houston, TX",
                    "linkedin_url": "https://linkedin.com/in/janesmith",
                    "summary": "ICU Registered Nurse with 8 years of critical care experience.",
                },
                "experience": [
                    {
                        "company": "Memorial Hermann Hospital",
                        "role": "RN - ICU",
                        "start_date": "03/15/2020",
                        "end_date": "Present",
                        "is_current": True,
                        "location": "6411 Fannin St, Houston, TX 77030",
                        "description": [
                            "Provided critical care nursing in a 12-bed MICU.",
                            "Coordinated care for ventilated patients.",
                        ],
                        "achievements": ["Charge nurse for 18 months"],
                    }
                ],
                "education": [
                    {
                        "institution": "UT Health Science Center",
                        "degree": "Bachelor of Science in Nursing",
                        "field_of_study": "Nursing",
                        "graduation_year": 2016,
                    }
                ],
                "skills": ["RN", "ICU", "NICU", "ACLS", "BLS"],
                "certifications": [
                    {"name": "ACLS", "issuer": "American Heart Association", "expiry_date": "06/2025"}
                ],
                "licenses": [
                    {"name": "Registered Nurse License", "license_type": "RN", "state": "FL",
                     "license_number": "RN9411204", "status": "Active"}
                ],
                "projects": [],
                "languages": ["English", "Spanish"],
                "references": [
                    {
                        "name": "Dr. Maria Alvarez",
                        "relationship": "ICU Nurse Manager",
                        "company": "Memorial Hermann Hospital",
                        "email": "m.alvarez@example.com",
                        "phone": "+1 555 010 2233",
                    }
                ],
                "awards": ["DAISY Award (2023)", "Employee of the Year (2021)"],
                "publications": [
                    "Smith J. (2022). Reducing CLABSI rates in the MICU. J Nursing Care, 14(2).",
                ],
            }
        }
    )

    personal_info:   PersonalInfo          = Field(default_factory=PersonalInfo, description="Candidate contact and identity information")
    experience:      list[ExperienceItem]  = Field(default_factory=list, description="Work experience entries, most recent first")
    education:       list[EducationItem]   = Field(default_factory=list, description="Education history")
    skills:          list[str]             = Field(default_factory=list, description="Clinical specialties, credentials, and skills (e.g. ICU, NICU, BLS, ACLS)")
    certifications:  list[CertificationItem] = Field(default_factory=list, description="Professional certifications (BLS, ACLS, CCRN, NRP…) — NOT state licenses")
    licenses:        list[LicenseItem]     = Field(default_factory=list, description="State/professional licenses (e.g. state RN/LPN/RT license with its number), kept separate from certifications")
    projects:        list[ProjectItem]     = Field(default_factory=list, description="Notable projects")
    languages:       list[str]             = Field(default_factory=list, description="Spoken/written languages")
    references:      list[ReferenceItem]   = Field(default_factory=list, description="Professional references, if explicitly listed (not 'available upon request')")
    awards:          list[str]             = Field(default_factory=list, description="Awards, honors, and recognitions (e.g. 'DAISY Award 2023', 'Summa Cum Laude', 'Employee of the Year')")
    publications:    list[str]             = Field(default_factory=list, description="Publications, posters, or research contributions, each as a single citation string")
    professional_associations: list[str]   = Field(default_factory=list, description="Professional associations, society memberships, committees, and collaboratives, each verbatim (e.g. 'Sigma Theta Tau International Honor Society of Nursing Member', 'Sepsis Clinical Services Committee', 'SJHS Sepsis Process Owner')")

    @field_validator("experience", "education", "certifications", "licenses", "projects", "references", mode="before")
    @classmethod
    def coerce_lists(cls, v: object) -> list:
        return _coerce_list(v)

    @field_validator("skills", "languages", "awards", "publications", "professional_associations", mode="before")
    @classmethod
    def coerce_string_lists(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [str(i).strip() for i in items if i and str(i).strip()]

