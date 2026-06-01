from app.services.parsing.section_detector import detect


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
