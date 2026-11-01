"""Optional offline English OCR for image documents and scanned PDF pages.

OCR runs locally on the conversion server, without an external OCR API.
"""

import io
import os
import shutil
import subprocess
import warnings
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps

MAX_OCR_PAGES = 8
MAX_OCR_PIXELS = 25_000_000
MAX_OCR_TEXT = 1_000_000


class OCRError(Exception):
    """Expected OCR failure with a safe user-facing message."""


def available() -> bool:
    return bool(shutil.which("tesseract"))


def _recognize_png(png: bytes) -> str:
    if not available():
        raise OCRError("Image text recognition is not available on this server.")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "3"],
            input=png,
            capture_output=True,
            check=False,
            timeout=12,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise OCRError("OCR took too long. Try a clearer or smaller image.") from exc
    if result.returncode:
        raise OCRError("OCR could not read this image. Try a clearer image or PDF.")
    if len(result.stdout) > MAX_OCR_TEXT * 4:
        raise OCRError("OCR output exceeds the text processing limit.")
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise OCRError("OCR returned invalid text. Try a different image.") from exc


def image_to_text(source: Path) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(source) as original:
                if original.width * original.height > MAX_OCR_PIXELS:
                    raise OCRError("This image exceeds the 25 megapixel OCR limit.")
                image = ImageOps.exif_transpose(original).convert("RGB")
                output = io.BytesIO()
                image.save(output, format="PNG")
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise OCRError("This image exceeds the 25 megapixel OCR limit.") from exc
    text = _recognize_png(output.getvalue())
    if not text:
        raise OCRError("OCR could not find readable text. Try a sharper image.")
    if len(text) > MAX_OCR_TEXT:
        raise OCRError("OCR output exceeds the text processing limit.")
    return text


def pdf_to_text(source: Path, *, max_pages: int = 300) -> str:
    """Extract native text, and OCR any scanned pages lacking selectable text."""
    parts: list[str] = []
    size = 0
    ocr_pages = 0
    with pymupdf.open(source) as document:
        if document.needs_pass:
            raise OCRError("Unlock this PDF before converting it.")
        if len(document) > max_pages:
            raise OCRError(f"PDF text extraction is limited to {max_pages} pages.")
        for page in document:
            text = page.get_text("text")
            if not text.strip():
                if not available():
                    raise OCRError(
                        "This PDF contains scanned pages. OCR is not available on this server."
                    )
                ocr_pages += 1
                if ocr_pages > MAX_OCR_PAGES:
                    raise OCRError(
                        f"OCR supports up to {MAX_OCR_PAGES} scanned pages per file. Split the PDF."
                    )
                scale = 1.7
                if round(page.rect.width * scale) * round(page.rect.height * scale) > MAX_OCR_PIXELS:
                    raise OCRError(
                        "A scanned PDF page is too large for safe OCR. Reduce its page size."
                    )
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale), alpha=False
                )
                text = _recognize_png(pixmap.tobytes("png"))
            size += len(text)
            if size > MAX_OCR_TEXT:
                raise OCRError("Extracted text exceeds the processing limit.")
            parts.append(text)
    text = "\n\n".join(parts)
    if not text.strip():
        raise OCRError(
            "No readable text was found. OCR could not recognize text on these pages."
        )
    return text
