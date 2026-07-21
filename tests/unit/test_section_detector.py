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


def test_mixed_credentials_heading_is_detected():
    """The real-world 'Professional Associations/Certifications/Licenses/Collaboratives'
    heading: 64 chars, slash-joined with no spaces, and not starting with a bare
    keyword - the single-keyword rule misses it, so compound detection must catch it."""
    assert _match_section_header(
        "Professional Associations/Certifications/Licenses/Collaboratives"
    ) == "certifications"


def test_slash_joined_header_without_spaces_is_detected():
    assert _match_section_header("Certifications/Licenses") == "certifications"
    assert _match_section_header("Certifications/Licenses/Collaboratives") == "certifications"


def test_standalone_associations_headers_bucket_as_credentials():
    for h in ("Professional Associations", "Affiliations", "Memberships", "Committees"):
        assert _match_section_header(h) == "certifications", h


def test_prose_with_association_words_is_not_a_header():
    # A duty line that merely mentions a committee/council is not a section header.
    assert _match_section_header("Managed the sepsis committee and stroke council") is None
    assert _match_section_header("Member of the rapid response team on nights") is None


def test_mixed_credentials_block_is_bucketed_together():
    resume = (
        "Katherine Driscoll, RN\n\n"
        "Education\n"
        "University at Buffalo- BSN, Class of 2015\n\n"
        "Professional Associations/Certifications/Licenses/Collaboratives\n"
        "Florida RN License #RN9411204\n"
        "CCRN Certification\n"
        "Sigma Theta Tau International Honor Society of Nursing Member\n"
        "Sepsis Clinical Services Committee\n"
    )
    sections = detect(resume)
    block = sections.get("certifications", "")
    assert "RN9411204" in block
    assert "CCRN Certification" in block
    assert "Sepsis Clinical Services Committee" in block
    # ...and it did not spill into the preceding education section.
    assert "RN9411204" not in sections.get("education", "")
