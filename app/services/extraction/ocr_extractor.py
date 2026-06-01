"""
Tiered OCR extraction:
  1. Tesseract (local, free) — tried first
  2. Amazon Textract (paid, high accuracy) — used when Tesseract confidence is low

Tesseract confidence threshold: if mean word confidence < 60, escalate to Textract.
"""

import io
from typing import Any

import boto3
import pytesseract
from PIL import Image, ImageFilter
from pdf2image import convert_from_bytes

from app.core.config import get_settings
from app.core.exceptions import OCRError
from app.core.logging import get_logger

log = get_logger(__name__)

_TESSERACT_CONFIDENCE_THRESHOLD = 60  # out of 100


def extract(content: bytes, filename: str) -> tuple[str, bool]:
    """
    Returns (extracted_text, textract_used).
    Tries Tesseract first; falls back to Textract if confidence is low.
    """
    images = _to_images(content, filename)
    text, confidence = _tesseract(images)

    if confidence >= _TESSERACT_CONFIDENCE_THRESHOLD:
        log.info("ocr_tesseract_success", confidence=confidence)
        return text, False

    log.info("ocr_escalating_to_textract", tesseract_confidence=confidence)
    text = _textract(content, filename)
    return text, True


def _to_images(content: bytes, filename: str) -> list[Image.Image]:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return convert_from_bytes(content, dpi=300)
    return [Image.open(io.BytesIO(content))]


def _preprocess(img: Image.Image) -> Image.Image:
    """Grayscale + mild sharpen improves Tesseract accuracy."""
    img = img.convert("L")
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _tesseract(images: list[Image.Image]) -> tuple[str, float]:
    pages: list[str] = []
    confidences: list[float] = []

    for img in images:
        preprocessed = _preprocess(img)
        data = pytesseract.image_to_data(preprocessed, output_type=pytesseract.Output.DICT)
        words = [
            (data["text"][i], int(data["conf"][i]))
            for i in range(len(data["text"]))
            if data["text"][i].strip() and int(data["conf"][i]) > 0
        ]
        if words:
            page_text = " ".join(w for w, _ in words)
            page_conf = sum(c for _, c in words) / len(words)
            pages.append(page_text)
            confidences.append(page_conf)

    overall_conf = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n\n".join(pages), overall_conf


def _textract(content: bytes, filename: str) -> str:
    settings = get_settings()
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    # Always hit real Textract even in dev (no LocalStack support for Textract)
    client = boto3.client("textract", **kwargs)

    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        # Textract async for multi-page PDFs — but for simplicity use sync (1-page PDFs)
        # For multi-page, we convert to images and call detect_document_text per page
        images = convert_from_bytes(content, dpi=300)
        pages: list[str] = []
        for img in images:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            pages.append(_textract_image(client, buf.getvalue()))
        return "\n\n".join(pages)

    return _textract_image(client, content)


def _textract_image(client: Any, image_bytes: bytes) -> str:
    try:
        resp = client.detect_document_text(Document={"Bytes": image_bytes})
        lines = [
            block["Text"]
            for block in resp.get("Blocks", [])
            if block["BlockType"] == "LINE"
        ]
        return "\n".join(lines)
    except Exception as exc:
        raise OCRError(f"Textract failed: {exc}") from exc
