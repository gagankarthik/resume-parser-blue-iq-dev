"""
Tests for the deterministic credential recovery backstop.

It rescues state licenses and professional-association memberships the AI parse
dropped - the failure mode seen on a scanned résumé whose mixed
"Professional Associations/Certifications/Licenses/Collaboratives" block the model
under-extracts. The pass must be ADDITIVE, CONSERVATIVE, and DEDUPED.
"""

from app.models.schemas import (
    CertificationItem,
    ExperienceItem,
    LicenseItem,
    ParsedResumeAI,
)
from app.services.normalization import credential_recovery

# The real credentials block (Katherine Driscoll résumé), as OCR would yield it.
_CRED_BLOCK = """Education
University at Buffalo- Buffalo, NY- Class of 2015: BSN

Professional Associations/Certifications/Licenses/Collaboratives
- Florida RN License #RN9411204
- CCRN Certification
- Emergency Neurological Life Support (ENLS) Certification
- BayCare Steps to Leadership Completed December 2024
- Sigma Theta Tau International Honor Society of Nursing Member
- American Association of Critical Care Nurses Member
- BLS/ACLS/PALS Certification
- Critical Care Collaborative
- Sepsis Clinical Services Committee
- Stroke and Code Blue Committee
- East Region Stroke Committee
- ICU Liberation Committee
- Glycemic Management Committee
- SJHS Sepsis Process Owner
- SJHS Stroke Committee
"""


def test_recovers_dropped_license_and_all_associations():
    # The model captured only the certifications; license + associations were lost.
    parsed = ParsedResumeAI(certifications=[
        CertificationItem(name="CCRN Certification"),
        CertificationItem(name="BLS/ACLS/PALS Certification"),
    ])
    credential_recovery.recover(_CRED_BLOCK, parsed)

    assert len(parsed.licenses) == 1
    lic = parsed.licenses[0]
    assert lic.license_number == "RN9411204"
    assert lic.license_type == "RN"

    assoc = parsed.professional_associations
    assert "Critical Care Collaborative" in assoc
    assert "Sepsis Clinical Services Committee" in assoc
    assert "SJHS Sepsis Process Owner" in assoc
    assert any("Sigma Theta Tau" in a for a in assoc)
    assert any("American Association of Critical Care Nurses Member" == a for a in assoc)
    assert len(assoc) == 10  # all 10 committees/memberships/collaboratives


def test_does_not_duplicate_what_the_model_already_captured():
    parsed = ParsedResumeAI(
        licenses=[LicenseItem(name="Florida RN License", license_type="RN",
                              license_number="RN9411204")],
        professional_associations=[
            "Critical Care Collaborative",
            "Sepsis Clinical Services Committee",
            "Stroke and Code Blue Committee",
            "East Region Stroke Committee",
            "ICU Liberation Committee",
            "Glycemic Management Committee",
            "SJHS Sepsis Process Owner",
            "SJHS Stroke Committee",
            "Sigma Theta Tau International Honor Society of Nursing Member",
            "American Association of Critical Care Nurses Member",
        ],
    )
    credential_recovery.recover(_CRED_BLOCK, parsed)
    # No duplicate license (same number) and no duplicate associations.
    assert len(parsed.licenses) == 1
    assert len(parsed.professional_associations) == 10


def test_does_not_grab_a_certification_issuer_as_an_association():
    """A cert issuer line like 'American Heart Association' contains 'Association' but
    is NOT a membership - it must not be recovered as a professional association."""
    text = (
        "Certifications\n"
        "- BLS Certification\n"
        "- American Heart Association\n"
        "- American Red Cross\n"
    )
    parsed = ParsedResumeAI(certifications=[CertificationItem(name="BLS Certification")])
    credential_recovery.recover(text, parsed)
    assert parsed.professional_associations == []


def test_association_recovery_is_scoped_to_the_credentials_section():
    """A duty bullet in the EXPERIENCE section that mentions a committee must not be
    pulled in as a membership - only the credentials section is scanned."""
    text = (
        "Experience\n"
        "Charge RN, Acme Hospital\n"
        "- Served on the sepsis committee and led code blue council rounds\n\n"
        "Certifications\n"
        "- CCRN Certification\n"
    )
    parsed = ParsedResumeAI(certifications=[CertificationItem(name="CCRN Certification")])
    credential_recovery.recover(text, parsed)
    assert parsed.professional_associations == []


def test_drivers_license_is_not_promoted_to_a_state_license():
    text = "Certifications\n- Driver's License\n- BLS Certification\n"
    parsed = ParsedResumeAI(certifications=[CertificationItem(name="BLS Certification")])
    credential_recovery.recover(text, parsed)
    assert parsed.licenses == []


def test_numbered_license_recovered_even_outside_the_credentials_section():
    """If extraction scrambles the license line out of the credentials heading, its
    number is still a strong enough signal to recover it from anywhere in the text."""
    text = (
        "Summary\n"
        "Critical care RN. Florida RN License #RN9411204.\n\n"
        "Skills\n- ICU\n- Telemetry\n"
    )
    parsed = ParsedResumeAI()
    credential_recovery.recover(text, parsed)
    assert len(parsed.licenses) == 1
    assert parsed.licenses[0].license_number == "RN9411204"


def test_no_text_is_a_safe_noop():
    parsed = ParsedResumeAI()
    credential_recovery.recover("", parsed)
    credential_recovery.recover("   \n  ", parsed)
    assert parsed.licenses == []
    assert parsed.professional_associations == []


def test_a_plain_duty_mention_of_a_license_word_is_not_recovered():
    """A sentence merely mentioning 'license' without a number, outside the credentials
    section, must not become a license entry."""
    text = (
        "Experience\n"
        "Staff Nurse, Acme\n"
        "- Maintained an active RN license throughout employment\n"
    )
    parsed = ParsedResumeAI(experience=[ExperienceItem(company="Acme", role="RN")])
    credential_recovery.recover(text, parsed)
    assert parsed.licenses == []
