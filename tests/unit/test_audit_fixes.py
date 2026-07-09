"""Regression tests for the production-audit fixes (Critical / High / Medium)."""

import asyncio

import pytest

from app.core.config import INSECURE_AUTH_SECRET_DEFAULT, Settings
from app.models.schemas.resume import ExtractionNote, ParsedResumeAI
from app.models.schemas.validators import _sanitize_url
from app.services.normalization.skills_validator import _is_clinical_skill
from app.services.parsing import rule_parser
from app.services.parsing.agents import base

# ── C1: per-event-loop client/semaphore (warm Lambda worker reuse) ─────────────

def test_client_and_semaphore_rebind_across_event_loops(monkeypatch):
    """The worker Lambda creates a fresh event loop per invocation. The cached
    client/semaphore must rebind to the new loop instead of staying bound to the
    previous (now-closed) one — which would raise on warm-container reuse."""
    class _DummyClient:
        def __init__(self, **_kw):
            pass

    monkeypatch.setattr(base, "AsyncOpenAI", _DummyClient)
    base._client = None
    base._semaphore = None
    base._bound_loop = None

    async def _grab():
        base._ensure_for_loop()
        return base._semaphore, base._bound_loop

    loop1 = asyncio.new_event_loop()
    try:
        sem1, bound1 = loop1.run_until_complete(_grab())
    finally:
        loop1.close()

    loop2 = asyncio.new_event_loop()
    try:
        sem2, bound2 = loop2.run_until_complete(_grab())

        async def _use_semaphore():
            async with base._get_semaphore():
                return True

        # Acquiring on the new loop must not raise "bound to a different event loop".
        assert loop2.run_until_complete(_use_semaphore()) is True
    finally:
        loop2.close()

    assert bound1 is not bound2
    assert sem1 is not sem2


# ── C2: fail closed on the default auth secret in production ───────────────────

def test_production_rejects_default_auth_secret():
    s = Settings(environment="production", auth_secret=INSECURE_AUTH_SECRET_DEFAULT)
    with pytest.raises(RuntimeError):
        s.assert_production_ready()


def test_production_accepts_strong_auth_secret():
    Settings(
        environment="production", auth_secret="0f3c-strong-random-secret-value"
    ).assert_production_ready()  # must not raise


def test_development_allows_default_secret():
    Settings(environment="development").assert_production_ready()  # must not raise


# ── C3: ExtractionNote sanitizes instead of crashing the whole parse ───────────

def test_extraction_note_confidence_clamped():
    assert ExtractionNote(field="f", reason="r", confidence=5).confidence == 1.0
    assert ExtractionNote(field="f", reason="r", confidence=-3).confidence == 0.0
    assert ExtractionNote(field="f", reason="r", confidence="oops").confidence == 0.5


def test_extraction_note_numeric_value_coerced_not_raised():
    note = ExtractionNote(field="experience[0].facility_beds", reason="r", value=30)
    assert note.value == "30"


def test_parsed_resume_survives_malformed_extraction_note():
    # An out-of-range confidence + numeric value must NOT raise and demote the
    # whole (otherwise good) parse to a rule-based partial.
    r = ParsedResumeAI(
        extraction_notes=[
            {"field": "experience[0].facility_beds", "value": 30, "confidence": 5, "reason": "x"}
        ]
    )
    assert r.extraction_notes[0].confidence == 1.0
    assert r.extraction_notes[0].value == "30"


# ── M17: URL sanitizer drops junk instead of fabricating a link ────────────────

@pytest.mark.parametrize("junk", ["N/A", "not provided", "available upon request", "none"])
def test_sanitize_url_rejects_junk(junk):
    assert _sanitize_url(junk) is None


def test_sanitize_url_promotes_bare_host():
    assert _sanitize_url("linkedin.com/in/jane") == "https://linkedin.com/in/jane"
    assert _sanitize_url("https://example.com/x") == "https://example.com/x"


# ── M18: a lone object is wrapped for object-lists, dropped for string-lists ───

def test_lone_object_wrapped_for_object_list():
    r = ParsedResumeAI(experience={"company": "Acme", "role": "RN"})
    assert len(r.experience) == 1
    assert r.experience[0].company == "Acme"


def test_lone_object_dropped_from_string_list():
    r = ParsedResumeAI(skills={"name": "ICU"})
    assert r.skills == []


# ── M10: bare Roman-numeral "IV" no longer a false-positive clinical skill ─────

def test_roman_numeral_iv_not_clinical():
    assert _is_clinical_skill("Trauma Level IV") is False
    assert _is_clinical_skill("Grade IV") is False


def test_real_iv_skills_still_clinical():
    assert _is_clinical_skill("IV Therapy") is True
    assert _is_clinical_skill("IV/PICC") is True


# ── Low: a bare year range must not be mistaken for a phone number ─────────────

def test_year_range_not_extracted_as_phone():
    assert rule_parser.extract("Registered Nurse 2015 - 2019 at Acme").phones == []


def test_real_phone_still_extracted():
    phones = rule_parser.extract("Reach me at 313-283-5671 anytime").phones
    assert any("313" in p for p in phones)


# ── Completeness fields: headline, secondary phone, education location ──────────

def test_personal_info_headline_and_secondary_phone():
    from app.models.schemas.resume import PersonalInfo
    p = PersonalInfo(
        full_name="Juason Allen",
        headline="Registered Nurse / Aesthetic Nurse Injector",
        phone="1(518) 986-8089",
        phone_secondary="1(518) 986-7774",
    )
    assert p.headline == "Registered Nurse / Aesthetic Nurse Injector"
    assert p.phone == "1(518) 986-8089"
    assert p.phone_secondary == "1(518) 986-7774"
    # phone_secondary is sanitized like phone (junk / too-short -> None)
    assert PersonalInfo(phone_secondary="n/a").phone_secondary is None


def test_education_item_location():
    from app.models.schemas.resume import EducationItem
    e = EducationItem(institution="Belanger School of Nursing", location="Schenectady, NY 12304")
    assert e.location == "Schenectady, NY 12304"


# ── Broadened clinical-skill recognition (previously unrecognized) ─────────────

@pytest.mark.parametrize("skill", [
    "AED Trained", "Acute Care Assessment", "NG Tube Insertion",
    "NIHSS stroke Assessment", "G-tube care", "Detox CIWA Scoring",
    "Seizure Precautions", "Care Plans", "Botulinum toxin injections",
    "Dermal Filler", "Accessing Implanted Ported Cath", "EMAR",
])
def test_more_clinical_skills_recognized(skill):
    assert _is_clinical_skill(skill) is True


@pytest.mark.parametrize("nonclinical", ["Time Management", "Microsoft Excel", "Trauma Level IV"])
def test_nonclinical_still_unrecognized(nonclinical):
    assert _is_clinical_skill(nonclinical) is False
