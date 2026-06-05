"""
Date and credential/specialty normalization tests.

Generic skill aliases (JS → JavaScript, postgres → PostgreSQL, etc.) were
intentionally removed when the parser became healthcare-only. Skill
normalization now uses the healthcare_taxonomy module — see
test_healthcare_normalizer.py for that coverage.
"""

from app.services.normalization.normalizer import (
    _normalize_date,
    _normalize_skills,
    _strip_name_credentials,
)

# ── Date normalization (MM/DD/YYYY; partial precision preserved) ──────────────
# Never invent a missing day or month — a month/year value stays month/year.

def test_date_iso_full_to_us():
    assert _normalize_date("2024-02-16") == "02/16/2024"


def test_date_iso_month_keeps_precision():
    # Month/year only → MM/YYYY (NOT padded to a fabricated day).
    assert _normalize_date("2023-05") == "05/2023"


def test_date_us_numeric_keeps_day():
    assert _normalize_date("2/16/2024") == "02/16/2024"


def test_date_month_name_with_day():
    assert _normalize_date("February 16, 2024") == "02/16/2024"


def test_date_month_name_no_day():
    assert _normalize_date("January 2023") == "01/2023"


def test_date_month_name_ordinal_day():
    assert _normalize_date("July 21st, 2019") == "07/21/2019"


def test_date_slash_month_year():
    assert _normalize_date("05/2023") == "05/2023"


def test_date_year_only():
    assert _normalize_date("2023") == "2023"


def test_date_present_passthrough():
    assert _normalize_date("Present") == "Present"


def test_date_unparseable_is_none():
    assert _normalize_date("sometime last year") is None


# ── Name credential stripping ─────────────────────────────────────────────────

def test_strip_name_comma_credentials():
    assert _strip_name_credentials("Jane Smith, RN BSN") == "Jane Smith"


def test_strip_name_space_credentials():
    assert _strip_name_credentials("John Doe RN") == "John Doe"


def test_strip_name_keeps_plain_name():
    assert _strip_name_credentials("Maria Garcia") == "Maria Garcia"


def test_strip_name_preserves_last_first():
    # "Last, First" must survive — the tail is not credential-like.
    assert _strip_name_credentials("Smith, Jane") == "Smith, Jane"


# ── Skill dedup (case-insensitive) ────────────────────────────────────────────

def test_skill_deduplication():
    """Same skill in different casing should be deduplicated."""
    skills = _normalize_skills(["Python", "python", "PYTHON"])
    assert len(skills) == 1


def test_unknown_skill_passes_through():
    """Skills not in the healthcare taxonomy are kept as-is."""
    skills = _normalize_skills(["Microsoft Office", "Spanish fluency"])
    assert "Microsoft Office" in skills
    assert "Spanish fluency" in skills


def test_healthcare_specialty_normalizes():
    """Healthcare abbreviations ARE normalized via the taxonomy."""
    skills = _normalize_skills(["ICU", "ER", "ACLS"])
    assert "Intensive Care Unit" in skills
    assert "Emergency Room" in skills
    # ACLS isn't in the abbreviation map — passes through unchanged
    assert "ACLS" in skills
