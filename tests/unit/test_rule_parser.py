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


def test_email_with_ocr_space_around_at_recovered():
    # Tesseract reads underlined hyperlinks with a stray space next to the @.
    text = "Katherine N. Driscoll\nKatherine.Driscoll@ Baycare.org\n(631) 903-2593"
    out = extract(text)
    assert out.emails == ["Katherine.Driscoll@Baycare.org"]


def test_strict_email_preferred_over_loose():
    out = extract("jane@example.com and noise @ not-an-email-context")
    assert out.emails == ["jane@example.com"]
