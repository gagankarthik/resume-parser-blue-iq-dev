"""
Date and credential/specialty normalization tests.

Generic skill aliases (JS → JavaScript, postgres → PostgreSQL, etc.) were
intentionally removed when the parser became healthcare-only. Skill
normalization now uses the healthcare_taxonomy module — see
test_healthcare_normalizer.py for that coverage.
"""

from app.services.normalization.normalizer import _normalize_date, _normalize_skills

# ── Date normalization ────────────────────────────────────────────────────────

def test_date_iso_passthrough():
    assert _normalize_date("2023-05") == "2023-05"


def test_date_month_name():
    assert _normalize_date("January 2023") == "2023-01"


def test_date_slash_format():
    assert _normalize_date("05/2023") == "2023-05"


def test_date_year_only():
    assert _normalize_date("2023") == "2023-01"


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
