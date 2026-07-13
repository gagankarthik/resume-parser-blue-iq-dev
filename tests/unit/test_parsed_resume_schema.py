"""
Schema-level tests for ParsedResumeAI list coercion and the awards/publications
fields. Validators must SANITIZE (never raise) so malformed LLM output cannot
crash the pipeline.
"""

from app.models.schemas import ParsedResumeAI


def test_awards_and_publications_extracted():
    parsed = ParsedResumeAI.model_validate(
        {
            "skills": ["ICU"],
            "awards": ["DAISY Award (2023)", "Employee of the Year (2021)"],
            "publications": ["Smith J. (2022). Reducing CLABSI rates. J Nursing Care."],
        }
    )
    assert parsed.awards == ["DAISY Award (2023)", "Employee of the Year (2021)"]
    assert len(parsed.publications) == 1


def test_awards_default_empty():
    parsed = ParsedResumeAI.model_validate({"skills": []})
    assert parsed.awards == []
    assert parsed.publications == []


def test_extraction_notes_round_trip():
    """extraction_notes must validate, survive a model_dump round trip (the
    persistence path), and default to [] when absent."""
    assert ParsedResumeAI.model_validate({"skills": []}).extraction_notes == []

    parsed = ParsedResumeAI.model_validate(
        {
            "skills": [],
            "extraction_notes": [
                {
                    "field": "experience[1].facility_beds",
                    "value": None,
                    "confidence": 0.0,
                    "reason": "'63 beds' appears only in the summary; 3 facilities listed",
                },
                {
                    "field": "experience[0].profession",
                    "value": "RN",
                    "confidence": 1.0,
                    "reason": "profession stated in the role heading",
                },
            ],
        }
    )
    assert len(parsed.extraction_notes) == 2
    n0 = parsed.extraction_notes[0]
    assert n0.value is None and n0.confidence == 0.0 and "63 beds" in n0.reason
    # Round trip through the dict form persisted to DynamoDB.
    reloaded = ParsedResumeAI.model_validate(parsed.model_dump())
    assert reloaded.extraction_notes[1].value == "RN"


def test_extraction_notes_malformed_is_sanitized_not_raised():
    """A non-list / junk value must not crash validation."""
    parsed = ParsedResumeAI.model_validate({"skills": [], "extraction_notes": None})
    assert parsed.extraction_notes == []


def test_awards_null_coerced_to_list():
    # LLM may emit null instead of [] - must coerce, not crash.
    parsed = ParsedResumeAI.model_validate({"awards": None, "publications": None})
    assert parsed.awards == []
    assert parsed.publications == []


def test_awards_strips_blanks_and_non_strings():
    parsed = ParsedResumeAI.model_validate(
        {"awards": ["  Honor  ", "", None, 123, "  "]}
    )
    assert parsed.awards == ["Honor", "123"]


def test_references_default_empty():
    parsed = ParsedResumeAI.model_validate({"skills": []})
    assert parsed.references == []


# -- Work History form fields on ExperienceItem -------------------------------

def test_experience_work_history_fields_round_trip():
    parsed = ParsedResumeAI.model_validate(
        {
            "experience": [
                {
                    "company": "Fort Sanders Regional Medical Center",
                    "role": "RN - Med Surg/Tele",
                    "start_date": "2026-01-01",
                    "end_date": "2026-04-30",
                    "city": "Knoxville",
                    "state": "Tennessee",
                    "country": "United States",
                    "zip_code": "37916",
                    "profession": "RN",
                    "specialties": ["Med Surg/ Tele"],
                    "charting_system": "Epic",
                    "shift": "Nights",
                }
            ]
        }
    )
    exp = parsed.experience[0]
    assert exp.city == "Knoxville"
    assert exp.zip_code == "37916"
    assert exp.profession == "RN"
    # specialties coerce to SpecialtyMatch objects (name only; id/confidence filled
    # later by the normalizer's matcher).
    assert [s.name for s in exp.specialties] == ["Med Surg/ Tele"]
    assert exp.specialties[0].specialty_id is None
    assert exp.charting_system == "Epic"


def test_experience_new_fields_default_null():
    exp = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "Y"}]}
    ).experience[0]
    assert exp.city is None
    assert exp.profession is None
    assert exp.specialties == []
    assert exp.teaching_facility is None


def test_experience_yes_no_na_coercion():
    exp = ParsedResumeAI.model_validate(
        {
            "experience": [
                {
                    "company": "X",
                    "role": "Y",
                    "teaching_facility": "yes",
                    "magnet_facility": "N/A",
                    "trauma_facility": "maybe",  # not yes/no/na → null
                }
            ]
        }
    ).experience[0]
    assert exp.teaching_facility == "Yes"
    assert exp.magnet_facility == "N/A"
    assert exp.trauma_facility is None


def test_experience_specialties_resolved_against_bundled_catalog():
    from app.services.normalization import specialty_catalog
    from app.services.normalization.normalizer import normalize

    specialty_catalog.reload(None)  # the bundled Gig snapshot (default catalog)
    try:
        parsed = ParsedResumeAI.model_validate(
            {"experience": [{"company": "X", "role": "RN", "profession": "RN",
                             "specialties": ["Med Surg/ Tele", "ICU"]}]}
        )
        normalize(parsed)
        specs = parsed.experience[0].specialties
        # The platform's EXACT names are preserved (never re-worded), each mapped
        # to a profession-scoped catalog id at full name-tier confidence.
        assert [s.name for s in specs] == ["Med Surg/ Tele", "ICU"]
        assert all(s.specialty_id and s.match_tier == "name" for s in specs)
        assert all(s.confidence == 1.0 for s in specs)
    finally:
        specialty_catalog.reload("")


def test_experience_specialty_objects_accepted():
    # The model now emits objects ({"name": ...}); a dict round-trip must work too.
    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "RN",
                         "specialties": [{"name": "ICU"}, "Med Surg/Tele"]}]}
    )
    specs = parsed.experience[0].specialties
    assert [s.name for s in specs] == ["ICU", "Med Surg/Tele"]


def test_specialty_name_strips_leaked_json_structure():
    # Smaller models sometimes leak JSON structural chars into the value under
    # structured output (observed in production: 'Med/Surg"}],'). The name must be
    # recovered clean so the matcher can map it.
    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "LPN",
                         "specialties": [{"name": 'Med/Surg"}],'}, {"name": 'Corrections"}],'}]}]}
    )
    assert [s.name for s in parsed.experience[0].specialties] == ["Med/Surg", "Corrections"]


# -- Dates: MM/DD/YYYY, partial precision preserved, never fabricated ----------

def test_experience_month_year_date_not_padded():
    exp = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "Y", "start_date": "August 2018", "end_date": "April '19"}]}
    ).experience[0]
    assert exp.start_date == "08/2018"   # NOT 08/01/2018
    assert exp.end_date == "04/2019"     # apostrophe = explicit 2-digit year


def test_ambiguous_bare_month_day_not_guessed():
    # "June 30" could be the 30th or June 2030 - a FUTURE year, so still ambiguous
    # and we don't guess.
    exp = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "Y", "start_date": "June 30"}]}
    ).experience[0]
    assert exp.start_date is None


def test_written_month_past_two_digit_year_is_a_year():
    # "August 2018 - April 19": the trailing 19 is 2019, a past year - not a day.
    # (Regression: this end date used to sanitize to None and get lost.)
    exp = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "Y",
                         "start_date": "August 2018", "end_date": "April 19"}]}
    ).experience[0]
    assert exp.start_date == "08/2018"
    assert exp.end_date == "04/2019"


def test_numeric_month_two_digit_year():
    exp = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "Y",
                         "start_date": "8/18", "end_date": "4/19"}]}
    ).experience[0]
    assert exp.start_date == "08/2018"
    assert exp.end_date == "04/2019"


def test_numeric_two_digit_year_allows_near_future_expiry():
    # Cert expiries are commonly written MM/YY a few years out - must parse.
    cert = ParsedResumeAI.model_validate(
        {"certifications": [{"name": "BLS", "expiry_date": "4/27"}]}
    ).certifications[0]
    assert cert.expiry_date == "04/2027"


def test_impossible_calendar_date_rejected():
    exp = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "Y", "start_date": "02/30/2024", "end_date": "04/31/2025"}]}
    ).experience[0]
    assert exp.start_date is None
    assert exp.end_date is None


def test_experience_full_date_us_format():
    exp = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "Y", "start_date": "2021-02-14", "end_date": "Present"}]}
    ).experience[0]
    assert exp.start_date == "02/14/2021"
    assert exp.end_date == "Present"


def test_experience_address_and_state_not_fabricated():
    exp = ParsedResumeAI.model_validate(
        {
            "experience": [
                {
                    "company": "Riverside Regional Medical Center",
                    "role": "Critical Care RN",
                    "location": "500 J Clyde Morris Blvd, Newport News, VA 23601",
                    "state": "VA",
                }
            ]
        }
    ).experience[0]
    assert exp.location == "500 J Clyde Morris Blvd, Newport News, VA 23601"
    assert exp.state == "VA"        # not expanded to "Virginia"
    assert exp.country is None      # not invented as "United States"


def test_experience_multi_sentence_bullet_stays_one_item():
    exp = ParsedResumeAI.model_validate(
        {
            "experience": [
                {
                    "company": "X",
                    "role": "Y",
                    "description": [
                        "Works in the Critical Care Unit/Cardiac Care Unit. Also worked in the SICU.",
                        "Collaborates with the interdisciplinary team.",
                    ],
                }
            ]
        }
    ).experience[0]
    assert exp.description[0] == "Works in the Critical Care Unit/Cardiac Care Unit. Also worked in the SICU."
    assert len(exp.description) == 2


def test_certification_bare_date_is_neutral_not_expiry():
    cert = ParsedResumeAI.model_validate(
        {"certifications": [{"name": "BLS", "date": "12/2024"}]}
    ).certifications[0]
    assert cert.date == "12/2024"
    assert cert.expiry_date is None
    assert cert.issued_date is None


# -- State licenses (kept separate from certifications) -----------------------

def test_state_license_round_trip():
    parsed = ParsedResumeAI.model_validate(
        {
            "licenses": [
                {
                    "name": "Registered Nurse License",
                    "license_type": "RN",
                    "state": "FL",
                    "license_number": "RN9411204",
                    "status": "Active",
                }
            ]
        }
    )
    lic = parsed.licenses[0]
    assert lic.license_type == "RN"
    assert lic.state == "FL"               # not expanded to "Florida"
    assert lic.license_number == "RN9411204"   # letter prefix preserved
    assert lic.is_compact is False
    assert lic.status == "Active"


def test_licenses_default_empty_and_null_coerced():
    assert ParsedResumeAI.model_validate({"skills": []}).licenses == []
    assert ParsedResumeAI.model_validate({"licenses": None}).licenses == []


def test_license_compact_flag_coerced_from_string():
    lic = ParsedResumeAI.model_validate(
        {"licenses": [{"name": "RN License", "is_compact": "multistate"}]}
    ).licenses[0]
    assert lic.is_compact is True


def test_license_missing_name_defaulted_not_dropped():
    # A licence with a number but no explicit name must not be discarded.
    lic = ParsedResumeAI.model_validate(
        {"licenses": [{"name": "", "license_number": "9411204"}]}
    ).licenses[0]
    assert lic.name == "Unknown License"
    assert lic.license_number == "9411204"


# -- Post-nominal credentials on personal_info --------------------------------

def test_personal_credentials_coerced_and_trimmed():
    p = ParsedResumeAI.model_validate(
        {"personal_info": {"full_name": "Jane Smith",
                           "credentials": ["  RN ", "", None, "BSN"]}}
    ).personal_info
    assert p.credentials == ["RN", "BSN"]


def test_personal_credentials_default_empty():
    p = ParsedResumeAI.model_validate({"personal_info": {"full_name": "Jane Smith"}}).personal_info
    assert p.credentials == []


# -- Professional associations (memberships / committees / collaboratives) ----

def test_professional_associations_round_trip():
    parsed = ParsedResumeAI.model_validate(
        {"professional_associations": [
            "Sigma Theta Tau International Honor Society of Nursing Member",
            "Sepsis Clinical Services Committee",
        ]}
    )
    assert len(parsed.professional_associations) == 2


def test_professional_associations_default_and_null_coerced():
    assert ParsedResumeAI.model_validate({"skills": []}).professional_associations == []
    assert ParsedResumeAI.model_validate(
        {"professional_associations": None}
    ).professional_associations == []
