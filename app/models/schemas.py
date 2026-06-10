"""
All Pydantic schemas used across the application.

Design principles:
  • ParsedResumeAI   — OpenAI structured output target. Validators SANITIZE (return
                       None/default) rather than raise, so malformed LLM output never
                       crashes the pipeline.
  • API I/O schemas  — Request/response models with strict validation and OpenAPI metadata.
  • ConfidenceScores — Per-section 0.0–1.0 scores.
"""

from __future__ import annotations

import re
from datetime import date as _date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── Shared validators ─────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_URL_RE   = re.compile(r"^https?://", re.I)

# Month-name → number, accepting 3-letter prefixes (jan, sept, etc.).
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _sanitize_email(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().lower()
    return v if _EMAIL_RE.match(v) else None


def _sanitize_url(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    if not _URL_RE.match(v):
        v = f"https://{v}"
    return v if len(v) <= 2048 else None


def _fmt_mdy(mo: int, day: int, year: int) -> str | None:
    # Reject impossible calendar dates (e.g. 02/30, 04/31) instead of echoing them.
    if not 1900 <= year <= 2035:
        return None
    try:
        _date(year, mo, day)
    except ValueError:
        return None
    return f"{mo:02d}/{day:02d}/{year}"


def _fmt_my(mo: int, year: int) -> str | None:
    if not (1 <= mo <= 12 and 1900 <= year <= 2035):
        return None
    return f"{mo:02d}/{year}"


def _expand_yy(yy: int) -> int:
    """Expand a 2-digit year using a 1969–2068 pivot (e.g. 19 → 2019, 98 → 1998)."""
    return 2000 + yy if yy < 69 else 1900 + yy


def _sanitize_date(v: str | None) -> str | None:
    """Normalize a date to MM/DD/YYYY, MM/YYYY, or YYYY — preserving the precision
    actually stated on the résumé. 'Present' passes through; unparseable → None.

    Crucially, this NEVER invents a missing day or month. A month/year value such
    as 'August 2018' becomes '08/2018' (not '08/01/2018'), and a year-only value
    stays 'YYYY'. Accepts ISO (YYYY-MM-DD / YYYY-MM), US numeric (M/D/YYYY,
    M/YYYY), and written forms ('Aug 2018', 'July 21, 2019', 'July 21st 2019').
    """
    if not v:
        return None
    v = str(v).strip()
    if not v:
        return None
    if v.lower() == "present":
        return "Present"

    # ISO full / year-month
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", v)
    if m:
        return _fmt_mdy(int(m[2]), int(m[3]), int(m[1]))
    m = re.match(r"^(\d{4})-(\d{1,2})$", v)
    if m:
        return _fmt_my(int(m[2]), int(m[1]))

    # US numeric full / month-year  (slash or hyphen separated)
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", v)
    if m:
        return _fmt_mdy(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2})$", v)
    if m:
        return _fmt_mdy(int(m[1]), int(m[2]), _expand_yy(int(m[3])))
    m = re.match(r"^(\d{1,2})[/-](\d{4})$", v)
    if m:
        return _fmt_my(int(m[1]), int(m[2]))
    # Numeric month + 2-digit year ("4/19" → 04/2019, "12/25" → 12/2025). On a
    # résumé this form is a month/year ("August 2018 – April 19"), not a
    # day-of-month without a year. A modest future window keeps cert expiry
    # dates ("exp 4/27") parseable.
    m = re.match(r"^(\d{1,2})[/-](\d{2})$", v)
    if m and 1 <= int(m[1]) <= 12:
        year = _expand_yy(int(m[2]))
        if year <= _date.today().year + 10:
            return _fmt_my(int(m[1]), year)

    # Written: "Month DD, YYYY" / "Month DDth YYYY"
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", v)
    if m and m[1][:3].lower() in _MONTHS:
        return _fmt_mdy(_MONTHS[m[1][:3].lower()], int(m[2]), int(m[3]))

    # Written: "Month YYYY"
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$", v)
    if m and m[1][:3].lower() in _MONTHS:
        return _fmt_my(_MONTHS[m[1][:3].lower()], int(m[2]))

    # Written with a 2-digit year: "Month 'YY" (apostrophe = explicit year),
    # "Month YY" where YY cannot be a day-of-month (>31), or "Month YY" where the
    # expanded year is in the past ("April 19" → 04/2019 — résumé date ranges end
    # in years, and a bare day-of-month with no year is useless anyway). Only a
    # value that would expand to a FUTURE year ("June 30" → 2030?) stays ambiguous,
    # so we do not guess — better None than a wrong date.
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+('?)(\d{2})$", v)
    if m and m[1][:3].lower() in _MONTHS:
        yy = int(m[3])
        year = _expand_yy(yy)
        if m[2] == "'" or yy > 31 or year <= _date.today().year:
            return _fmt_my(_MONTHS[m[1][:3].lower()], year)

    # Year only
    m = re.match(r"^(\d{4})$", v)
    if m:
        year = int(m[1])
        return str(year) if 1900 <= year <= 2035 else None

    return None


def _sanitize_phone(v: str | None) -> str | None:
    """Keep phones with a plausible digit count (7–15), preserving format.

    Counts TOTAL digits, not consecutive ones — a formatted number like
    '(555) 234-5678' has no run of 6+ digits yet is perfectly valid.
    """
    if not v:
        return None
    v = v.strip()
    digit_count = len(re.sub(r"\D", "", v))
    return v if 7 <= digit_count <= 15 else None


def _sanitize_year(v: int | None) -> int | None:
    if v is None:
        return None
    return v if 1900 <= v <= 2035 else None


def _sanitize_str(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    return v if v else None


def _sanitize_yes_no_na(v: str | None) -> str | None:
    """Coerce a tri-state facility flag to exactly 'Yes', 'No', or 'N/A'.

    Anything that doesn't clearly read as yes/no/na (including blanks) becomes
    None so an unstated flag is never invented.
    """
    if not v:
        return None
    key = v.strip().lower().replace(".", "").replace("/", "")
    if key in {"yes", "y", "true"}:
        return "Yes"
    if key in {"no", "n", "false"}:
        return "No"
    if key in {"na", "n a", "notapplicable", "none"}:
        return "N/A"
    return None


def _coerce_list(v) -> list:
    """Ensure list fields are always lists — guards against LLM returning null."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return []


# ── Resume data models (OpenAI structured output target) ──────────────────────

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
                "specialties": ["Med Surg / Tele"],
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
    specialties:   list[str]     = Field(default_factory=list, description="Clinical specialties/units for this role (e.g. 'Med Surg/Tele', 'ICU'). One per item.")
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
    def coerce_specialties(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [s for i in items if isinstance(i, str) and (s := i.strip())]

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
            return [s for p in parts if (s := re.sub(r"^[-*]\s+", "", p).strip())]
        items = _coerce_list(v)
        return [s for i in items if isinstance(i, str) and (s := i.strip())]

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
    awards:          list[str]             = Field(default_factory=list, description="Awards, honors, and recognitions (e.g. 'DAISY Award 2023', 'Employee of the Year')")
    publications:    list[str]             = Field(default_factory=list, description="Publications, posters, or research contributions, each as a single citation string")

    @field_validator("experience", "education", "certifications", "licenses", "projects", "references", mode="before")
    @classmethod
    def coerce_lists(cls, v: object) -> list:
        return _coerce_list(v)

    @field_validator("skills", "languages", "awards", "publications", mode="before")
    @classmethod
    def coerce_string_lists(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [str(i).strip() for i in items if i and str(i).strip()]


# ── Confidence scores ─────────────────────────────────────────────────────────

class ConfidenceScores(BaseModel):
    overall:       float = Field(..., ge=0.0, le=1.0, description="Weighted overall confidence (0.0 = no data, 1.0 = complete)")
    personal_info: float = Field(..., ge=0.0, le=1.0, description="Confidence in personal contact fields")
    experience:    float = Field(..., ge=0.0, le=1.0, description="Confidence in experience entries")
    education:     float = Field(..., ge=0.0, le=1.0, description="Confidence in education entries")
    skills:        float = Field(..., ge=0.0, le=1.0, description="Confidence in skills / specialties list")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "overall": 0.91,
                "personal_info": 0.96,
                "experience": 0.88,
                "education": 0.90,
                "skills": 1.0,
            }
        }
    )


# ── Skills validation ─────────────────────────────────────────────────────────

class SkillsValidation(BaseModel):
    """
    Validation of the parsed `skills` against the healthcare taxonomy.

    Each skill is classified as either *recognized* (it maps to a canonical
    specialty, profession/credential, or known clinical certification) or
    *unrecognized* (free-form, out-of-taxonomy). Use `recognized_ratio` to flag
    records whose skills could not be grounded and may need human review.
    """

    total:              int            = Field(..., ge=0, description="Total distinct skills validated")
    recognized_count:   int            = Field(..., ge=0, description="Skills matched to the healthcare taxonomy")
    unrecognized_count: int            = Field(..., ge=0, description="Free-form skills with no taxonomy match")
    recognized_ratio:   float          = Field(..., ge=0.0, le=1.0, description="recognized_count / total (0.0–1.0)")
    recognized:         list[str]      = Field(default_factory=list, description="Canonical names of recognized skills")
    unrecognized:       list[str]      = Field(default_factory=list, description="Skills not found in the taxonomy")
    groups:             dict[str, str] = Field(default_factory=dict, description="Recognized specialty → group label (e.g. 'Intensive Care Unit' → 'ICU')")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total": 5,
                "recognized_count": 4,
                "unrecognized_count": 1,
                "recognized_ratio": 0.8,
                "recognized": [
                    "Intensive Care Unit",
                    "Neonatal Intensive Care Unit",
                    "Registered Nurse",
                    "ACLS",
                ],
                "unrecognized": ["Patient Advocacy"],
                "groups": {
                    "Intensive Care Unit": "ICU",
                    "Neonatal Intensive Care Unit": "Nursery",
                },
            }
        }
    )


# ── API request / response schemas ───────────────────────────────────────────

class ParseResponse(BaseModel):
    """Response for POST /api/v1/resume/parse"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "status": "completed",
                "data": {"personal_info": {}, "experience": [], "education": [], "skills": []},
                "confidence": {"overall": 0.91, "personal_info": 0.96, "experience": 0.88, "education": 0.90, "skills": 1.0},
                "poll_url": None,
            }
        }
    )

    job_id:            str                        = Field(..., description="Unique job identifier (ULID)")
    status:            str                        = Field(..., description="'completed' for sync jobs, 'processing' for async (OCR) jobs")
    data:              ParsedResumeAI | None   = Field(None, description="Parsed resume data — present when status is 'completed'")
    confidence:        ConfidenceScores | None = Field(None, description="Per-section confidence scores — present when status is 'completed'")
    skills_validation: SkillsValidation | None = Field(None, description="Skills validated against the healthcare taxonomy — present when status is 'completed'")
    partial:           bool                       = Field(False, description="True when parsing degraded — `data` holds only what could be recovered (e.g. contact anchors) and needs human review. See `warnings`.")
    warnings:          list[str]                  = Field(default_factory=list, description="Non-fatal issues detected during parsing (e.g. AI parse failed and a partial record was returned). Empty on a clean parse.")
    poll_url:          str | None              = Field(None, description="Polling URL — present when status is 'processing'")


class UploadUrlRequest(BaseModel):
    """Request body for POST /api/v1/resume/upload-url"""

    model_config = ConfigDict(
        json_schema_extra={"example": {"filename": "jane_smith_rn.pdf"}}
    )

    filename: str = Field(
        ...,
        description="Original filename including extension (.pdf, .docx, .png, .jpg, .jpeg, .tiff, .webp)",
        examples=["jane_smith_rn.pdf"],
    )

    @field_validator("filename")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("filename is required")
        return v


class UploadUrlResponse(BaseModel):
    """Response for POST /api/v1/resume/upload-url.

    Use this for files larger than ~6 MB. POST the file as multipart form data to
    `upload_url`, including every key in `fields` plus a `file` field, then call
    `parse_url` with the returned `job_id`.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "upload_url": "https://resume-parser-blue-iq-temp.s3.amazonaws.com/",
                "fields": {
                    "key": "temp/01J3K5M2N4P6Q8R0S2T4U6V8W0/jane_smith_rn.pdf",
                    "x-amz-server-side-encryption": "AES256",
                    "policy": "<base64-policy>",
                    "x-amz-signature": "<signature>",
                },
                "s3_key": "temp/01J3K5M2N4P6Q8R0S2T4U6V8W0/jane_smith_rn.pdf",
                "max_file_size_mb": 10,
                "expires_in_seconds": 900,
                "parse_url": "/api/v1/resume/parse-uploaded",
            }
        }
    )

    job_id:             str            = Field(..., description="Job identifier (ULID) — pass this to parse-uploaded")
    upload_url:         str            = Field(..., description="S3 endpoint to POST the file to (multipart form data)")
    fields:             dict[str, str] = Field(..., description="Form fields that must accompany the upload, exactly as given")
    s3_key:             str            = Field(..., description="The S3 object key the file will be stored under")
    max_file_size_mb:   int            = Field(..., description="Maximum accepted file size; larger uploads are rejected by S3")
    expires_in_seconds: int            = Field(..., description="Seconds until the upload URL expires")
    parse_url:          str            = Field(..., description="Endpoint to call (with job_id) once the upload completes")


class ParseUploadedRequest(BaseModel):
    """Request body for POST /api/v1/resume/parse-uploaded"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0", "force_textract": False}
        }
    )

    job_id: str = Field(..., description="The job_id returned by /resume/upload-url")
    force_textract: bool = Field(
        False,
        description="Skip Tesseract and use AWS Textract directly for any OCR this "
                    "file needs (scanned PDF/image, or a digital PDF with a broken "
                    "text layer). Higher accuracy on hard scans, higher cost.",
    )

    @field_validator("job_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("job_id is required")
        return v


class JobStatusResponse(BaseModel):
    """Response for GET /api/v1/resume/job/{job_id}"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "status": "completed",
                "data": None,
                "confidence": None,
                "error": None,
            }
        }
    )

    job_id:            str                        = Field(..., description="Job identifier")
    status:            str                        = Field(..., description="pending | processing | completed | failed")
    data:              ParsedResumeAI | None   = Field(None, description="Parsed data — set when status is 'completed'")
    confidence:        ConfidenceScores | None = Field(None, description="Confidence scores — set when status is 'completed'")
    skills_validation: SkillsValidation | None = Field(None, description="Skills validated against the healthcare taxonomy — set when status is 'completed'")
    partial:           bool                       = Field(False, description="True when parsing degraded — `data` holds only what could be recovered and needs human review. See `warnings`.")
    warnings:          list[str]                  = Field(default_factory=list, description="Non-fatal issues detected during parsing. Empty on a clean parse.")
    error:             str | None              = Field(None, description="Error description — set when status is 'failed'")


class WebhookCreateRequest(BaseModel):
    """Request body for POST /api/v1/webhooks"""

    url:    str        = Field(
        ...,
        description="HTTPS URL that will receive webhook POST requests",
        examples=["https://your-server.com/hooks/resume"],
    )
    events: list[str]  = Field(
        ...,
        description="Events to subscribe to",
        examples=[["parse.completed", "parse.failed", "batch.completed"]],
        min_length=1,
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not _URL_RE.match(v):
            raise ValueError("Webhook URL must begin with http:// or https://")
        if len(v) > 2048:
            raise ValueError("Webhook URL too long")
        return v

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        valid = {"parse.completed", "parse.failed", "batch.completed"}
        invalid = set(v) - valid
        if invalid:
            raise ValueError(f"Invalid events: {sorted(invalid)}. Valid: {sorted(valid)}")
        return v


class WebhookResponse(BaseModel):
    """Response for webhook endpoints"""

    webhook_id:   str           = Field(..., description="Webhook identifier")
    url:          str           = Field(..., description="Delivery URL")
    events:       list[str]     = Field(..., description="Subscribed events")
    hmac_secret:  str | None = Field(None, description="HMAC signing secret — only returned on creation")
    status:       str           = Field(..., description="active | disabled")
    created_at:   str           = Field(..., description="ISO 8601 creation timestamp")


class ErrorDetail(BaseModel):
    """Uniform error envelope used by all 4xx/5xx responses"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "status_code": 413,
                    "error_code":  "FILE_TOO_LARGE",
                    "detail":      "File size 12288 KB exceeds the 10 MB limit",
                    "hint":        "Please reduce the file size or split the document and try again.",
                    "request_id":  "8f3a-b212-4c7e-9d1f-a8b3c0e1d2f4",
                }
            }
        }
    )

    status_code:  int = Field(..., description="HTTP status code")
    error_code:   str = Field(..., description="Machine-readable error identifier (e.g. FILE_TOO_LARGE)")
    detail:       str = Field(..., description="Developer-readable error description")
    hint:         str = Field(..., description="User-facing actionable message — display this to your end user")
    request_id:   str = Field(..., description="Request ID — include in support tickets")


class HealthResponse(BaseModel):
    status:        str            = Field(..., description="'ok' or 'degraded'")
    version:       str            = Field(..., description="API version")
    environment:   str            = Field(..., description="development | production")
    latency_ms:    int | None  = Field(None, description="Dependency probe round-trip time in ms")
    dependencies:  dict | None = Field(None, description="Per-dependency status: ok | unreachable")


class RetryResponse(BaseModel):
    """Response for POST /api/v1/resume/{job_id}/retry"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "01J3K5M3N5P7Q9R1S3T5U7V9W1",
                "original_job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "retry_count": 1,
                "status": "completed",
                "data": None,
                "confidence": None,
                "poll_url": None,
            }
        }
    )

    job_id:            str                        = Field(..., description="New job ID for this retry attempt")
    original_job_id:   str                        = Field(..., description="The job ID that was retried")
    retry_count:       int                        = Field(..., description="How many times this job has been retried (1 = first retry)")
    status:            str                        = Field(..., description="completed | processing")
    data:              ParsedResumeAI | None   = Field(None, description="Parsed data — set when status is completed")
    confidence:        ConfidenceScores | None = Field(None, description="Confidence scores — set when status is completed")
    skills_validation: SkillsValidation | None = Field(None, description="Skills validated against the healthcare taxonomy — set when status is completed")
    partial:           bool                       = Field(False, description="True when parsing degraded — `data` holds only what could be recovered and needs human review. See `warnings`.")
    warnings:          list[str]                  = Field(default_factory=list, description="Non-fatal issues detected during parsing. Empty on a clean parse.")
    poll_url:          str | None              = Field(None, description="Polling URL — set for async retries")


# ── Feedback schemas ──────────────────────────────────────────────────────────

# Reject feedback whose JSON would overflow a single DynamoDB item (400 KB hard
# limit). Parser output is normally a few KB, so this only catches abuse.
_MAX_FEEDBACK_JSON_BYTES = 350_000


class FeedbackRequest(BaseModel):
    """
    Request body for POST /api/v1/resume/{job_id}/feedback.

    Sent server-to-server after a user reviews and corrects a parsed resume.
    Both `original` and `updated` are stored verbatim (free-form JSON, not
    re-sanitised) so the corrections retain full fidelity for model improvement.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "original": {"personal_info": {"full_name": "Jane Smith"}, "skills": ["ICU"]},
                "updated": {"personal_info": {"full_name": "Jane A. Smith, RN"}, "skills": ["ICU", "CCRN"]},
                "changed": True,
                "profile_id": "gig-profile-8821",
                "notes": "User fixed the name suffix and added a certification.",
            }
        }
    )

    original: dict[str, Any] = Field(
        ..., description="The original parser JSON, exactly as returned by /resume/parse"
    )
    updated: dict[str, Any] = Field(
        ..., description="The user-corrected JSON (post-review)"
    )
    changed: bool | None = Field(
        None,
        description="Whether the user changed anything. If omitted, it is derived from the diff.",
    )
    profile_id: str | None = Field(
        None, max_length=200, description="Optional client-side profile/record identifier"
    )
    notes: str | None = Field(
        None, max_length=2000, description="Optional free-form reviewer notes"
    )

    @model_validator(mode="after")
    def _check_size(self) -> FeedbackRequest:
        import json

        size = len(json.dumps({"original": self.original, "updated": self.updated}).encode())
        if size > _MAX_FEEDBACK_JSON_BYTES:
            raise ValueError(
                f"Feedback payload too large ({size} bytes); "
                f"limit is {_MAX_FEEDBACK_JSON_BYTES} bytes"
            )
        return self


class FeedbackResponse(BaseModel):
    """Response for POST /api/v1/resume/{job_id}/feedback (accepted asynchronously)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "feedback_id": "01J3K5M9N1P3Q5R7S9T1U3V5W7",
                "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "status": "accepted",
                "changed": True,
                "changed_fields": ["personal_info.full_name", "skills[1]"],
                "created_at": "2026-06-03T12:34:56.000000+00:00",
            }
        }
    )

    feedback_id:    str       = Field(..., description="Unique identifier for this feedback record (ULID)")
    job_id:         str       = Field(..., description="The parse job this feedback relates to")
    status:         str       = Field(..., description="Always 'accepted' — feedback is processed asynchronously")
    changed:        bool      = Field(..., description="Whether any field differed between original and updated")
    changed_fields: list[str] = Field(..., description="Dotted paths of the leaf fields that changed")
    created_at:     str       = Field(..., description="ISO 8601 timestamp the feedback was recorded")


# ── Batch schemas ─────────────────────────────────────────────────────────────

class BatchSkipped(BaseModel):
    filename:  str = Field(..., description="Original filename")
    reason:    str = Field(..., description="Why this file was rejected")


class BatchSubmitResponse(BaseModel):
    """Response for POST /api/v1/resume/batch"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "batch_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "total": 148,
                "skipped": 2,
                "skipped_files": [
                    {"filename": "notes.txt", "reason": "Unsupported file extension '.txt'"},
                ],
                "job_ids": ["01J3K5M2...", "01J3K5M3..."],
                "status": "processing",
                "poll_url": "/api/v1/resume/batch/01J3K5M2N4P6Q8R0S2T4U6V8W0",
            }
        }
    )

    batch_id:       str               = Field(..., description="Batch identifier")
    total:          int               = Field(..., description="Number of files accepted for processing")
    skipped:        int               = Field(..., description="Number of files rejected at upload time")
    skipped_files:  list[BatchSkipped] = Field(default_factory=list, description="Details of rejected files")
    job_ids:        list[str]         = Field(..., description="Job IDs for accepted files, in submission order")
    status:         str               = Field(..., description="Always 'processing' — results arrive via webhook or polling")
    poll_url:       str               = Field(..., description="URL to poll for overall batch status")


class BatchStatusResponse(BaseModel):
    """Response for GET /api/v1/resume/batch/{batch_id}"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "batch_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "status": "partial",
                "total": 150,
                "completed": 145,
                "failed": 5,
                "processing": 0,
                "created_at": "2026-06-01T10:00:00+00:00",
                "completed_at": "2026-06-01T10:08:42+00:00",
            }
        }
    )

    batch_id:       str           = Field(..., description="Batch identifier")
    status:         str           = Field(..., description="processing | completed | partial | failed")
    total:          int           = Field(..., description="Total files in batch")
    completed:      int           = Field(..., description="Successfully parsed files")
    failed:         int           = Field(..., description="Files that failed parsing")
    processing:     int           = Field(..., description="Files still in progress (total - completed - failed)")
    created_at:     str           = Field(..., description="ISO 8601 timestamp when batch was submitted")
    completed_at:   str | None = Field(None, description="ISO 8601 timestamp when all files finished")
