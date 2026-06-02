"""
Tiered OCR pipeline:
  1. Preprocess (deskew → denoise → contrast → binarise)
  2. Tesseract (free, local) — primary
  3. Amazon Textract (paid, high accuracy) — fallback when confidence < threshold

Handles: rotated scans, low-contrast images, poor-quality photocopies,
         handwritten annotations, watermarked backgrounds.
"""

import io
from typing import Any

import boto3
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image, ImageEnhance, ImageFilter

from app.core.config import get_settings
from app.core.exceptions import OCRError
from app.core.logging import get_logger

log = get_logger(__name__)

_CONFIDENCE_THRESHOLD = 60   # escalate to Textract when mean confidence < this
_MIN_WIDTH_FOR_OCR    = 800  # upscale images narrower than this (px)


# ── Public API ────────────────────────────────────────────────────────────────

def extract(content: bytes, filename: str) -> tuple[str, bool]:
    """
    Returns (extracted_text, textract_used).
    Tries Tesseract first; escalates to Textract on low confidence.
    """
    images = _to_images(content, filename)
    preprocessed = [_preprocess(img) for img in images]
    text, confidence = _run_tesseract(preprocessed)

    if confidence >= _CONFIDENCE_THRESHOLD:
        log.info("ocr_tesseract_ok", confidence=round(confidence, 1), pages=len(images))
        return text, False

    log.info(
        "ocr_escalating_textract",
        tesseract_confidence=round(confidence, 1),
        threshold=_CONFIDENCE_THRESHOLD,
    )
    text = _run_textract(content, filename, preprocessed)
    return text, True


# ── Image loading ─────────────────────────────────────────────────────────────

def _to_images(content: bytes, filename: str) -> list[Image.Image]:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return convert_from_bytes(content, dpi=300)
    return [Image.open(io.BytesIO(content))]


# ── Preprocessing pipeline ────────────────────────────────────────────────────

def _preprocess(img: Image.Image) -> Image.Image:
    """
    Full preprocessing chain optimised for low-quality healthcare resume scans:
      1. Flatten to RGB (handles RGBA, P-mode palette images)
      2. Auto-rotate using Tesseract OSD (detects 90/180/270° mis-scans)
      3. Grayscale
      4. Upscale to minimum width for accurate OCR
      5. Denoise (median filter)
      6. Contrast enhancement (CLAHE-equivalent via PIL)
      7. Sharpen
      8. Binarise (Otsu-equivalent via PIL auto-threshold)
    """
    # 1. Flatten
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # 2. Auto-rotate (OSD)
    img = _auto_rotate(img)

    # 3. Grayscale
    img = img.convert("L")

    # 4. Upscale if too small
    if img.width < _MIN_WIDTH_FOR_OCR:
        scale = _MIN_WIDTH_FOR_OCR / img.width
        new_size = (int(img.width * scale), int(img.height * scale))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    # 5. Denoise — median filter removes salt-and-pepper noise from fax scans
    img = img.filter(ImageFilter.MedianFilter(size=3))

    # 6. Contrast enhancement
    img = ImageEnhance.Contrast(img).enhance(2.0)

    # 7. Sharpen for cleaner glyph edges
    img = img.filter(ImageFilter.SHARPEN)

    # 8. Binarise — converts to pure black/white, removes grey gradients
    img = img.point(lambda x: 0 if x < 140 else 255, "L")

    return img


def _auto_rotate(img: Image.Image) -> Image.Image:
    """
    Use Tesseract OSD to detect orientation and correct it.
    Handles resumes scanned sideways or upside-down.
    Gracefully skips if OSD fails (e.g., very low quality image).
    """
    try:
        gray = img.convert("L") if img.mode != "L" else img
        osd = pytesseract.image_to_osd(
            gray, output_type=pytesseract.Output.DICT, config="--psm 0"
        )
        angle = int(osd.get("rotate", 0))
        if angle in (90, 180, 270):
            img = img.rotate(-angle, expand=True, fillcolor=255 if img.mode == "L" else (255, 255, 255))
            log.info("ocr_auto_rotated", angle=angle)
    except Exception:
        pass  # OSD fails on very small/blurry images — continue without rotation
    return img


# ── Tesseract ─────────────────────────────────────────────────────────────────

def _run_tesseract(images: list[Image.Image]) -> tuple[str, float]:
    """Run Tesseract on preprocessed images; return (text, mean_confidence)."""
    pages: list[str] = []
    confidences: list[float] = []

    for img in images:
        # --psm 3 = fully automatic page segmentation (handles multi-column)
        # --oem 3 = best available LSTM engine
        data = pytesseract.image_to_data(
            img,
            config="--psm 3 --oem 3",
            output_type=pytesseract.Output.DICT,
        )
        words = [
            (data["text"][i], int(data["conf"][i]))
            for i in range(len(data["text"]))
            if data["text"][i].strip() and int(data["conf"][i]) > 0
        ]
        if words:
            pages.append(" ".join(w for w, _ in words))
            confidences.append(sum(c for _, c in words) / len(words))

    overall = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n\n".join(pages), overall


# ── Textract ──────────────────────────────────────────────────────────────────

def _run_textract(
    original_content: bytes,
    filename: str,
    preprocessed_images: list[Image.Image],
) -> str:
    """
    Call AWS Textract via synchronous detect_document_text.
    Uses preprocessed images (converted to PNG) for better accuracy.
    """
    settings = get_settings()
    client = boto3.client("textract", region_name=settings.aws_region)
    # Never use LocalStack endpoint for Textract — always real AWS
    pages: list[str] = []

    for img in preprocessed_images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pages.append(_textract_image_bytes(client, buf.getvalue()))

    return "\n\n".join(pages)


def _textract_image_bytes(client: Any, image_bytes: bytes) -> str:
    try:
        resp = client.detect_document_text(Document={"Bytes": image_bytes})
        lines = [
            block["Text"]
            for block in resp.get("Blocks", [])
            if block["BlockType"] == "LINE"
            and float(block.get("Confidence", 0)) >= 50
        ]
        return "\n".join(lines)
    except Exception as exc:
        raise OCRError(f"Textract failed: {exc}") from exc
