"""
Unit tests for OCR image loading (`_to_images`).

Covers the multi-page TIFF fix: every frame must be returned for OCR, not just
the first page. Single-frame formats (PNG/JPG/WEBP) return exactly one image.
These tests only exercise image loading — no Tesseract/Textract calls.
"""

import io

from PIL import Image

from app.services.extraction.ocr_extractor import _to_images


def test_multipage_tiff_returns_all_frames():
    buf = io.BytesIO()
    page1 = Image.new("RGB", (20, 20), "white")
    page2 = Image.new("RGB", (20, 20), "black")
    page3 = Image.new("RGB", (20, 20), "gray")
    page1.save(buf, format="TIFF", save_all=True, append_images=[page2, page3])

    images = _to_images(buf.getvalue(), "scan.tiff")

    assert len(images) == 3


def test_single_frame_png_returns_one_image():
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buf, format="PNG")

    images = _to_images(buf.getvalue(), "scan.png")

    assert len(images) == 1
