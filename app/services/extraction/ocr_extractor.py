"""
Tiered OCR pipeline:
  1. Preprocess (deskew → denoise → contrast → binarise)
  2. Tesseract (free, local) — primary
  3. Amazon Textract (paid, high accuracy) — fallback when confidence < threshold

Handles: rotated scans, low-contrast images, poor-quality photocopies,
         handwritten annotations, watermarked backgrounds.
"""

import io
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image, ImageEnhance, ImageFilter, ImageSequence

from app.core.config import get_settings
from app.core.exceptions import OCRError
from app.core.logging import get_logger

log = get_logger(__name__)

_CONFIDENCE_THRESHOLD = 60   # escalate to Textract when mean confidence < this
_MIN_WIDTH_FOR_OCR    = 800  # upscale images narrower than this (px)


# ── Public API ────────────────────────────────────────────────────────────────

def extract(content: bytes, filename: str, force_textract: bool = False) -> tuple[str, bool]:
    """
    Returns (extracted_text, textract_used).

    Default (tiered): tries Tesseract first; escalates to Textract on low
    confidence. When ``force_textract`` is True — or the global
    ``settings.force_textract`` flag is set — Tesseract is skipped and the scan
    goes straight to Textract for maximum accuracy.
    """
    force = force_textract or get_settings().force_textract

    images = _to_images(content, filename)
    preprocessed = [_preprocess(img) for img in images]

    if force:
        log.info("ocr_forced_textract", pages=len(images))
        return _run_textract(preprocessed), True

    text, confidence = _run_tesseract(preprocessed)

    if confidence >= _CONFIDENCE_THRESHOLD:
        log.info("ocr_tesseract_ok", confidence=round(confidence, 1), pages=len(images))
        return text, False

    log.info(
        "ocr_escalating_textract",
        tesseract_confidence=round(confidence, 1),
        threshold=_CONFIDENCE_THRESHOLD,
    )
    text = _run_textract(preprocessed)
    return text, True


# ── Image loading ─────────────────────────────────────────────────────────────

def _to_images(content: bytes, filename: str) -> list[Image.Image]:
    ext = filename.rsplit(".", 1)[-1].lower()
    max_pages = max(1, get_settings().ocr_max_pages)
    if ext == "pdf":
        # last_page bounds work INSIDE pdftoppm, so we never rasterize (and hold in
        # memory) more than max_pages at 300 DPI — an unbounded scan would OOM.
        images = convert_from_bytes(content, dpi=300, first_page=1, last_page=max_pages)
        if len(images) >= max_pages:
            log.info("ocr_page_cap_applied", max_pages=max_pages)
        return images
    # Iterate every frame so multi-page TIFFs are OCR'd in full, not just page 1.
    # Single-frame formats (PNG/JPG/WEBP) yield exactly one image. Bounded the same.
    img = Image.open(io.BytesIO(content))
    frames: list[Image.Image] = []
    for frame in ImageSequence.Iterator(img):
        if len(frames) >= max_pages:
            log.info("ocr_page_cap_applied", max_pages=max_pages)
            break
        frames.append(frame.copy())
    return frames


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
    """Run Tesseract on preprocessed images; return (text, mean_confidence).

    Bails out after the FIRST page when its confidence is already below the
    escalation threshold: scan quality is uniform across a document, so OCRing
    the remaining pages just to discard the whole result doubles the OCR time
    on exactly the documents that are already slow (multi-page bad scans).
    """
    pages: list[str] = []
    confidences: list[float] = []

    for i, img in enumerate(images):
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

        if i == 0 and len(images) > 1:
            first_conf = confidences[0] if confidences else 0.0
            if first_conf < _CONFIDENCE_THRESHOLD:
                log.info(
                    "ocr_tesseract_early_escalation",
                    first_page_confidence=round(first_conf, 1),
                    pages_skipped=len(images) - 1,
                )
                return "", first_conf

    overall = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n\n".join(pages), overall


# ── Textract ──────────────────────────────────────────────────────────────────

def _run_textract(preprocessed_images: list[Image.Image]) -> str:
    """
    Call AWS Textract via synchronous detect_document_text.
    Uses preprocessed images (converted to PNG) for better accuracy.
    Pages are sent concurrently — they are independent network calls, and a
    serial loop made multi-page scans pay full Textract latency per page.
    """
    settings = get_settings()
    client = boto3.client("textract", region_name=settings.aws_region)
    # Never use LocalStack endpoint for Textract — always real AWS

    def _one_page(img: Image.Image) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return _textract_image_bytes(client, buf.getvalue())

    if len(preprocessed_images) == 1:
        return _one_page(preprocessed_images[0])

    # boto3 clients are thread-safe for concurrent calls; page order is preserved.
    with ThreadPoolExecutor(max_workers=min(4, len(preprocessed_images))) as pool:
        pages = list(pool.map(_one_page, preprocessed_images))
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
