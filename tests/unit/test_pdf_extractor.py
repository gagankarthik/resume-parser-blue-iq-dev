"""PDF extractor reading-order tests.

Synthetic PDFs are built with PyMuPDF at explicit coordinates so we can assert
the column-vs-row reading logic without committing binary fixtures.
"""

import fitz  # PyMuPDF

from app.services.extraction import pdf_extractor


def _make_pdf(items: list[tuple[float, float, str]], width: float = 595, height: float = 842) -> bytes:
    """Build a one-page PDF placing each `text` with its baseline at (x, y)."""
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    for x, y, text in items:
        page.insert_text((x, y), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def test_right_aligned_dates_stay_with_their_entries():
    # A single-column resume with a right-aligned date strip: each date shares a
    # row (y) with its entry. The detached-column bug read all dates last; the
    # row-aware path must keep every date next to its entry.
    pdf = _make_pdf([
        (50, 100, "Work Experience"),
        (50, 140, "Travel Nurse - Host HealthCare"),   (460, 140, "April 2025 - present"),
        (50, 170, "Provided care in fast-paced units and led teams."),
        (50, 210, "Staff Nurse - Sisters of Charity"),  (460, 210, "Dec 2023 - Oct 2025"),
        (50, 240, "Coordinated patient assessments and care plans."),
        (50, 290, "Education"),
        (50, 330, "ASN - Trocaire College"),            (460, 330, "2020 - 2022"),
        (50, 370, "BBA - Medaille College"),            (460, 370, "2015 - 2018"),
    ])
    text = pdf_extractor.extract(pdf)

    # Each date follows its own entry and precedes the next one.
    assert text.index("Travel Nurse") < text.index("April 2025") < text.index("Staff Nurse")
    assert text.index("BBA - Medaille") < text.index("2015 - 2018")
    # The Education date must not have drifted up before its degree, nor down to
    # the page bottom (the original detached-column failure mode).
    assert text.index("ASN - Trocaire") < text.index("2020 - 2022")
    assert text.index("April 2025") < text.index("Education")


def test_true_two_column_layout_reads_column_wise():
    # A dense sidebar (skills) beside a dense main column (experience). Both
    # columns carry comparable text, so this is NOT a sparse annotation strip and
    # must be read column-by-column, never interleaved row-by-row.
    pdf = _make_pdf([
        (50, 100, "Skills"),
        (50, 130, "Patient Care"),
        (50, 160, "IV Therapy"),
        (50, 190, "Triage"),
        (50, 220, "Charting"),
        (320, 100, "Experience"),
        (320, 135, "Registered Nurse at General Hospital"),
        (320, 170, "Delivered bedside care to many patients."),
        (320, 205, "Charge Nurse at City Clinic"),
        (320, 240, "Supervised nursing staff and schedules."),
    ])
    text = pdf_extractor.extract(pdf)

    # Whole left column comes before the right column; not interleaved.
    assert text.index("Charting") < text.index("Experience")
    assert text.index("Patient Care") < text.index("Registered Nurse")


def test_sparse_prose_second_column_is_not_treated_as_annotations():
    # The hardened case: a genuine right-hand text column that is *sparse* (fewer
    # blocks than the left) and happens to row-align with left blocks. Because its
    # blocks are prose (long, multi-word, no dates) it must NOT be merged row-wise
    # - it stays an independent column read after the left one.
    pdf = _make_pdf([
        (50, 100, "Profile"),
        (50, 130, "Registered nurse with a decade of experience."),
        (50, 160, "Skilled in acute and critical care settings."),
        (50, 190, "Known for compassionate, evidence-based practice."),
        (50, 220, "Committed to mentoring junior staff members."),
        (50, 250, "Bilingual in English and Spanish for patients."),
        (50, 280, "Maintains active BLS and ACLS certifications."),
        # Sparse right column of prose, each line aligned to a left row.
        (340, 130, "Led a unit through a hospital accreditation review."),
        (340, 190, "Reduced patient fall rates across the medical ward."),
        (340, 250, "Coordinated discharge planning for complex cases."),
    ])
    text = pdf_extractor.extract(pdf)

    # Read column-wise: the whole left column precedes the right column, not
    # interleaved row-by-row.
    assert text.index("Maintains active BLS") < text.index("Led a unit")
    assert text.index("evidence-based") < text.index("Reduced patient fall")


def test_single_column_reads_top_to_bottom():
    pdf = _make_pdf([
        (50, 100, "Jane Doe"),
        (50, 130, "Summary"),
        (50, 160, "Experienced registered nurse."),
    ])
    text = pdf_extractor.extract(pdf)

    assert text.index("Jane Doe") < text.index("Summary") < text.index("Experienced")
