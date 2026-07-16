from app.services.parsing.rule_parser import extract


def test_extracts_email():
    result = extract("Contact me at john.doe@example.com for details.")
    assert "john.doe@example.com" in result.emails


def test_extracts_linkedin():
    result = extract("linkedin.com/in/johndoe")
    assert any("johndoe" in u for u in result.linkedin_urls)


def test_extracts_github():
    result = extract("github.com/johndoe")
    assert any("johndoe" in u for u in result.github_urls)


def test_extracts_phone():
    result = extract("Call me at +1 (555) 123-4567")
    assert len(result.phones) >= 1


def test_no_false_positives_on_clean_text():
    result = extract("This resume has no contact information at all.")
    assert result.emails == []
    assert result.phones == []


def test_ignores_equipment_model_number_as_phone():
    # "LTV 950-1200" is a ventilator model range mined from a skills line; its
    # "950-1200" is 7 digits with no country code and must NOT become a phone anchor
    # (it previously surfaced as a bogus phone_secondary).
    result = extract("Heated High Flow systems, Multiple vent brands (Drager, Servo, 840, LTV 950-1200, Trilogy)")
    assert result.phones == []


def test_keeps_real_number_amid_model_noise():
    # A genuine 10-digit contact number is still extracted even next to model noise.
    result = extract("Servo-U, LTV 950-1200. Call (563) 213-9245.")
    assert any("563" in p and "9245" in p for p in result.phones)


def test_drops_bare_seven_digit_local_number_without_country_code():
    # A bare 7-digit local number (no area code, no "+") is uncallable noise.
    assert extract("ref 555-1234 in the notes").phones == []


def test_email_with_ocr_space_around_at_recovered():
    # Tesseract reads underlined hyperlinks with a stray space next to the @.
    text = "Katherine N. Driscoll\nKatherine.Driscoll@ Baycare.org\n(631) 903-2593"
    out = extract(text)
    assert out.emails == ["Katherine.Driscoll@Baycare.org"]


def test_strict_email_preferred_over_loose():
    out = extract("jane@example.com and noise @ not-an-email-context")
    assert out.emails == ["jane@example.com"]
