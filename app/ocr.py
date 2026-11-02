"""Bounded, local Tesseract OCR for scanned PDF-to-Word conversion."""

import csv
import os
import shutil
import statistics
import subprocess
from functools import lru_cache
from pathlib import Path

MAX_OCR_PAGES = 10
MAX_OCR_PIXELS = 8_000_000
MAX_OCR_WORDS = 50_000
OCR_DPI = 200


@lru_cache(maxsize=1)
def installed_languages() -> tuple[str, ...]:
    executable = shutil.which("tesseract")
    if not executable:
        return ()
    try:
        result = subprocess.run(
            [executable, "--list-langs"], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode:
        return ()
    return tuple(line.strip() for line in result.stdout.splitlines()[1:] if line.strip())


def ocr_available() -> bool:
    return "eng" in installed_languages()


def needs_ocr(page) -> bool:
    text = page.get_text().strip()
    images = page.get_image_info()
    if not text:
        return bool(images or page.get_drawings())
    # Scans can have a small digital header/footer over an image of the page.
    # Recognize those images too, rather than returning only the header text.
    area = page.rect.width * page.rect.height
    return any(
        max(0, image["bbox"][2] - image["bbox"][0]) * max(0, image["bbox"][3] - image["bbox"][1])
        > area * 0.5
        for image in images
    )


def recognize_page(page, directory: Path, index: int) -> list[list[dict]]:
    import pymupdf

    from app.pdf_word import PDFWordError

    scale = OCR_DPI / 72
    if page.rect.width * page.rect.height * scale**2 > MAX_OCR_PIXELS:
        raise PDFWordError(
            "A scanned page exceeds the 8 megapixel OCR limit. Resize this PDF first."
        )
    image = directory / f"ocr-page-{index}.png"
    output = directory / f"ocr-result-{index}"
    try:
        pixels = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY)
        pixels.save(image)
        del pixels
        try:
            result = subprocess.run(
                [
                    shutil.which("tesseract"),
                    str(image),
                    str(output),
                    "-l",
                    "eng",
                    "--psm",
                    "1" if "osd" in installed_languages() else "3",
                    "tsv",
                ],
                capture_output=True,
                check=False,
                timeout=20,
                env={**os.environ, "OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1"},
            )
        except subprocess.TimeoutExpired as error:
            raise PDFWordError(
                "OCR took too long on a page. Try a smaller, clearer scan."
            ) from error
        tsv = output.with_suffix(".tsv")
        if result.returncode or not tsv.is_file():
            raise PDFWordError("OCR could not read this page. Try a clearer, upright scan.")
        if tsv.stat().st_size > 8 * 1024 * 1024:
            raise PDFWordError(
                "OCR results exceed the safe processing limit. Split this PDF first."
            )
        lines = {}
        with tsv.open(encoding="utf-8", errors="replace", newline="") as stream:
            for row in csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE):
                if row["level"] != "5" or not row["text"].strip():
                    continue
                text = "".join(char for char in row["text"] if char.isprintable())
                if not text:
                    continue
                key = (row["block_num"], row["par_num"], row["line_num"])
                lines.setdefault(key, []).append(
                    {
                        "text": text,
                        "confidence": float(row["conf"]),
                        "height": int(row["height"]) * 72 / OCR_DPI,
                    }
                )
        return list(lines.values())
    finally:
        image.unlink(missing_ok=True)
        output.with_suffix(".tsv").unlink(missing_ok=True)


def native_lines(page) -> list[list[dict]]:
    """Keep existing digital text rather than OCRing it a second time."""
    import pymupdf

    lines = []
    for block in page.get_text(
        "dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    )["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            text = "".join(span["text"] for span in line["spans"])
            text = "".join(char for char in text if char.isprintable()).strip()
            if text:
                lines.append(
                    [
                        {
                            "text": text,
                            "confidence": 100,
                            "height": statistics.median(s["size"] for s in line["spans"]) * 0.75,
                        }
                    ]
                )
    return lines


def ocr_docx(pdf, target: Path) -> None:
    from docx import Document
    from docx.enum.section import WD_ORIENT, WD_SECTION_START
    from docx.enum.text import WD_COLOR_INDEX
    from docx.shared import Pt

    from app.pdf_word import MAX_TEXT, PDFWordError

    scanned = [index for index, page in enumerate(pdf) if needs_ocr(page)]
    if len(scanned) > MAX_OCR_PAGES:
        raise PDFWordError("OCR supports up to 10 scanned pages per file. Split this PDF first.")
    if scanned and not ocr_available():
        raise PDFWordError(
            "OCR is unavailable on this server. Install Tesseract with English language data, "
            "or choose Exact appearance to keep scanned pages as images."
        )
    document = Document()
    document.styles["Normal"].font.name = "Arial"
    words = characters = 0
    for index, page in enumerate(pdf):
        width, height = page.rect.width, page.rect.height
        if min(width, height) < 36 or max(width, height) > 1584:
            raise PDFWordError("This PDF has a page size that Word cannot represent reliably.")
        section = (
            document.sections[0] if not index else document.add_section(WD_SECTION_START.NEW_PAGE)
        )
        section.page_width, section.page_height = Pt(width), Pt(height)
        section.orientation = WD_ORIENT.LANDSCAPE if width > height else WD_ORIENT.PORTRAIT
        margin = min(36, width / 10, height / 10)
        section.left_margin = section.right_margin = Pt(margin)
        section.top_margin = section.bottom_margin = Pt(margin)
        lines = (
            recognize_page(page, target.parent, index) if index in scanned else native_lines(page)
        )
        for line in lines:
            words += len(line)
            characters += sum(len(word["text"]) for word in line)
            if words > MAX_OCR_WORDS or characters > MAX_TEXT:
                raise PDFWordError("OCR text exceeds the processing limit. Split this PDF first.")
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(3)
            size = min(24, max(8, statistics.median(word["height"] for word in line) / 0.75))
            for word_index, word in enumerate(line):
                if word_index:
                    paragraph.add_run(" ")
                run = paragraph.add_run(word["text"])
                run.font.size = Pt(round(size * 2) / 2)
                if word["confidence"] < 60:
                    run.font.highlight_color = WD_COLOR_INDEX.YELLOW
        if not lines:
            document.add_paragraph()  # Retain blank pages within a mixed document.
    if not characters:
        raise PDFWordError(
            "OCR found no readable text. Try a clearer scan or choose Exact appearance."
        )
    document.save(target)
