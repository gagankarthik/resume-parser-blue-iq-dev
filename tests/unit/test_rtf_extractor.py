from app.services.extraction import rtf_extractor

_SAMPLE_RTF = (
    b"{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 Times New Roman;}}\n"
    b"\\f0\\fs24 Jane Smith, RN\\par\n"
    b"jane.smith@example.com\\par\n"
    b"\\b Experience\\b0\\par\n"
    b"Registered Nurse \\endash  ICU\\par\n"
    b"}"
)


def test_extracts_plain_text_and_paragraphs():
    text = rtf_extractor.extract(_SAMPLE_RTF)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert "Jane Smith, RN" in lines
    assert "jane.smith@example.com" in lines
    assert "Experience" in lines
    # control words (\b, \fs24, \par) must not leak into the output
    assert "\\" not in text


def test_decodes_escaped_non_ascii():
    # \'e9 is the cp1252 hex escape for "é"
    rtf = b"{\\rtf1\\ansi Jos\\'e9 Garc\\'eda\\par}"
    text = rtf_extractor.extract(rtf)
    assert "José García" in text


def test_malformed_rtf_degrades_gracefully():
    # striprtf is lenient: a truncated/unbalanced document still yields the
    # text it can recover instead of raising.
    text = rtf_extractor.extract(b"{\\rtf1\\ansi Partial resume text")
    assert "Partial resume text" in text
