"""
PDF Processor Service

Handles two paths:
  1. Digital/text-based PDF  → PyMuPDF direct text extraction
  2. Scanned/image-based PDF → PyMuPDF → page PNGs → Surya OCR → text

Decision: if the PDF yields fewer than `settings.pdf_text_threshold` characters
of extractable text, it is treated as scanned and run through Surya OCR.
"""

import io
import logging
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from PIL import Image

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Surya OCR model singletons — models are ~500MB-1GB, load once per process.

_surya_models: Optional[dict] = None


def _load_surya_models() -> dict:
    global _surya_models
    if _surya_models is not None:
        return _surya_models

    logger.info("Loading Surya OCR models (first call only – may take ~30 s)…")
    try:
        from surya.model.detection.model import load_model as load_det_model
        from surya.model.detection.model import load_processor as load_det_processor
        from surya.model.recognition.model import load_model as load_rec_model
        from surya.model.recognition.processor import load_processor as load_rec_processor

        det_processor = load_det_processor()
        det_model = load_det_model()
        rec_model = load_rec_model()
        rec_processor = load_rec_processor()

        _surya_models = {
            "det_model": det_model,
            "det_processor": det_processor,
            "rec_model": rec_model,
            "rec_processor": rec_processor,
        }
        logger.info("Surya OCR models loaded successfully.")
    except ImportError as e:
        logger.error("surya-ocr is not installed: %s", e)
        raise RuntimeError(
            "surya-ocr package is required for scanned PDF processing. "
            "Run: pip install surya-ocr"
        ) from e

    return _surya_models


def _run_surya_ocr(images: list[Image.Image]) -> str:
    """Run Surya OCR on a list of PIL images and return stitched text."""
    from surya.ocr import run_ocr

    models = _load_surya_models()
    langs = [["en"]] * len(images)

    predictions = run_ocr(
        images,
        langs,
        models["det_model"],
        models["det_processor"],
        models["rec_model"],
        models["rec_processor"],
    )

    page_texts: list[str] = []
    for page_pred in predictions:
        lines = [line.text for line in page_pred.text_lines]
        page_texts.append("\n".join(lines))

    return "\n\n--- PAGE BREAK ---\n\n".join(page_texts)


# Public API

class PDFProcessingResult:
    def __init__(
        self,
        raw_text: str,
        ocr_method: str,     # "direct" | "surya_ocr"
        page_count: int,
    ):
        self.raw_text = raw_text
        self.ocr_method = ocr_method
        self.page_count = page_count


def process_pdf(file_path: str | Path) -> PDFProcessingResult:
    """
    Synchronous PDF processing pipeline.
    Called from a background thread via asyncio.to_thread().
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = file_path.suffix.lower()

    # Direct image upload (JPEG / PNG)
    if suffix in {".jpg", ".jpeg", ".png"}:
        logger.info("Processing direct image file: %s", file_path.name)
        img = Image.open(file_path).convert("RGB")
        raw_text = _run_surya_ocr([img])
        return PDFProcessingResult(raw_text=raw_text, ocr_method="surya_ocr", page_count=1)

    # PDF path
    doc = fitz.open(str(file_path))
    page_count = len(doc)

    # Try direct text extraction first
    all_text_parts: list[str] = []
    for page in doc:
        all_text_parts.append(page.get_text("text"))
    direct_text = "\n\n--- PAGE BREAK ---\n\n".join(all_text_parts)
    total_chars = len(direct_text.strip())

    if total_chars >= settings.pdf_text_threshold:
        logger.info(
            "%s → text-based PDF (%d chars extracted directly).",
            file_path.name, total_chars,
        )
        doc.close()
        return PDFProcessingResult(
            raw_text=direct_text, ocr_method="direct", page_count=page_count
        )

    # Not enough text → treat as scanned, run Surya OCR
    logger.info(
        "%s → scanned PDF (%d chars found, below threshold %d). Running Surya OCR…",
        file_path.name, total_chars, settings.pdf_text_threshold,
    )
    images: list[Image.Image] = []
    for page in doc:
        # Render at 150 DPI for a good balance of quality vs. speed on CPU
        mat = fitz.Matrix(150 / 72, 150 / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        img_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        images.append(img)

    doc.close()

    raw_text = _run_surya_ocr(images)
    return PDFProcessingResult(
        raw_text=raw_text, ocr_method="surya_ocr", page_count=page_count
    )
