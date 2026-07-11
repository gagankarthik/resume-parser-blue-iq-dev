from app.services.parsing.section_detector import _match_section_header, detect

SAMPLE_RESUME = """
John Doe
john@example.com

Summary
Experienced software engineer with 5 years in backend development.

Experience
Software Engineer at Acme Corp
2020 - 2023
Built microservices in Python.

Education
Bachelor of Science in Computer Science
State University, 2019

Skills
Python, FastAPI, PostgreSQL, AWS
"""


def test_detects_summary_section():
    sections = detect(SAMPLE_RESUME)
    assert "summary" in sections


def test_detects_experience_section():
    sections = detect(SAMPLE_RESUME)
    assert "experience" in sections


def test_detects_education_section():
    sections = detect(SAMPLE_RESUME)
    assert "education" in sections


def test_detects_skills_section():
    sections = detect(SAMPLE_RESUME)
    assert "skills" in sections


def test_fallback_full_text_on_no_sections():
    sections = detect("John Doe, Software Engineer, john@example.com")
    assert "full_text" in sections


def test_compound_headers_recognised():
    # A compound header ("Education & Training") must be recognised as its section so
    # the degrees under it are labelled EDUCATION, not spilled into the prior block.
    assert _match_section_header("Education & Training") == "education"
    assert _match_section_header("Education and Certifications") == "education"
    assert _match_section_header("Licenses / Certifications") == "certifications"
    assert _match_section_header("Awards, Honors") == "achievements"


def test_prose_line_not_mistaken_for_header():
    # A summary sentence that merely STARTS with a section keyword is not a header.
    assert _match_section_header("Experience in a 64-bed ICU with a 1:2 ratio") is None
    assert _match_section_header("Skilled in critical care and telemetry nursing") is None


def test_compound_education_header_buckets_degrees():
    resume = (
        "Jane Smith, RN\n\n"
        "Work Experience\n"
        "Staff Nurse, Mercy Hospital\n\n"
        "Education & Training\n"
        "BSN, State University, 2018\n"
        "ADN, City College, 2015\n"
    )
    sections = detect(resume)
    assert "education" in sections
    assert "BSN" in sections["education"] and "ADN" in sections["education"]
