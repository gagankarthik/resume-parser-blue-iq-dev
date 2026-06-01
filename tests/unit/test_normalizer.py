from app.services.normalization.normalizer import _normalize_date, _normalize_skills


def test_date_iso_passthrough():
    assert _normalize_date("2023-05") == "2023-05"


def test_date_month_name():
    assert _normalize_date("January 2023") == "2023-01"


def test_date_slash_format():
    assert _normalize_date("05/2023") == "2023-05"


def test_date_year_only():
    assert _normalize_date("2023") == "2023-01"


def test_skill_normalization():
    skills = _normalize_skills(["nodejs", "JS", "postgres", "AWS"])
    assert "Node.js" in skills
    assert "JavaScript" in skills
    assert "PostgreSQL" in skills
    assert "AWS" in skills


def test_skill_deduplication():
    skills = _normalize_skills(["Python", "python", "PYTHON"])
    assert len(skills) == 1
