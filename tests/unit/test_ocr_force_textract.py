"""
Unit tests for the force-Textract option and the digital-PDF OCR quality
fallback.

  • ocr_extractor.extract(force_textract=True) skips Tesseract and calls Textract.
  • The global settings.force_textract flag has the same effect.
  • pipeline._is_low_quality_pdf_text flags broken/garbled text layers but trusts
    a clean résumé text layer.
"""

import io

from PIL import Image

from app.services.extraction import ocr_extractor
from app.services.pipeline import _is_low_quality_pdf_text


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, format="PNG")
    return buf.getvalue()


# ── force_textract routing ────────────────────────────────────────────────────

def test_force_textract_param_skips_tesseract(monkeypatch):
    calls = {"tesseract": 0, "textract": 0}

    def fake_tesseract(images):
        calls["tesseract"] += 1
        return "tess", 99.0

    def fake_textract(images):
        calls["textract"] += 1
        return "textract text"

    monkeypatch.setattr(ocr_extractor, "_run_tesseract", fake_tesseract)
    monkeypatch.setattr(ocr_extractor, "_run_textract", fake_textract)

    text, used = ocr_extractor.extract(_png_bytes(), "scan.png", force_textract=True)

    assert text == "textract text"
    assert used is True
    assert calls["tesseract"] == 0
    assert calls["textract"] == 1


def test_global_force_textract_flag_skips_tesseract(monkeypatch):
    calls = {"tesseract": 0, "textract": 0}

    monkeypatch.setattr(
        ocr_extractor, "_run_tesseract",
        lambda images: (calls.__setitem__("tesseract", calls["tesseract"] + 1), ("t", 99.0))[1],
    )
    monkeypatch.setattr(
        ocr_extractor, "_run_textract",
        lambda images: (calls.__setitem__("textract", calls["textract"] + 1), "x")[1],
    )

    settings = ocr_extractor.get_settings()
    monkeypatch.setattr(settings, "force_textract", True)

    _text, used = ocr_extractor.extract(_png_bytes(), "scan.png")

    assert used is True
    assert calls["tesseract"] == 0
    assert calls["textract"] == 1


def test_default_uses_tesseract_when_confident(monkeypatch):
    calls = {"tesseract": 0, "textract": 0}

    monkeypatch.setattr(
        ocr_extractor, "_run_tesseract",
        lambda images: (calls.__setitem__("tesseract", calls["tesseract"] + 1), ("good text", 95.0))[1],
    )
    monkeypatch.setattr(
        ocr_extractor, "_run_textract",
        lambda images: (calls.__setitem__("textract", calls["textract"] + 1), "x")[1],
    )

    text, used = ocr_extractor.extract(_png_bytes(), "scan.png")

    assert text == "good text"
    assert used is False
    assert calls["tesseract"] == 1
    assert calls["textract"] == 0


# ── digital-PDF quality heuristic ─────────────────────────────────────────────

def test_clean_resume_text_is_high_quality():
    text = (
        "Jane Smith, RN BSN\n"
        "Registered Nurse with 8 years of ICU experience at Memorial Hermann.\n"
        "Skills: ICU, NICU, ACLS, BLS. Education: University of Texas, 2016.\n"
        "Phone: (555) 234-5678  Email: jane.smith@example.com"
    )
    assert _is_low_quality_pdf_text(text) is False


def test_too_short_text_is_low_quality():
    assert _is_low_quality_pdf_text("Jane Smith RN") is True


def test_cid_artifact_text_is_low_quality():
    garbled = " ".join(f"(cid:{i})" for i in range(40))
    assert _is_low_quality_pdf_text(garbled) is True


def test_symbol_soup_is_low_quality():
    # A long string dominated by non-wordy symbols (broken glyph extraction).
    soup = "□▯■�críticoÿÿ □▯ ###@@@ %%% ^^^ &&& *** ((( ))) " * 8
    assert _is_low_quality_pdf_text(soup) is True


def test_replacement_chars_are_low_quality():
    junk = ("�" * 200)
    assert _is_low_quality_pdf_text(junk) is True
