"""Isolated PDF → Word worker, invoked by app.conversion with a hard timeout."""

import argparse
import json
import logging
from pathlib import Path

from app.pdf_errors import PDFWordError

MAX_PAGES = 50
MAX_TEXT = 1_000_000
MAX_PAGE_PIXELS = 25_000_000
MAX_IMAGE_BYTES = 48 * 1024 * 1024
logger = logging.getLogger(__name__)


def inspect_pdf(document, mode: str) -> bool:
    if document.needs_pass:
        raise PDFWordError("Unlock this PDF before converting it.")
    if not 0 < len(document) <= MAX_PAGES:
        raise PDFWordError(f"PDF-to-Word supports up to {MAX_PAGES} pages. Split this PDF first.")
    characters = 0
    has_text = False
    for page in document:
        text = page.get_text("text")
        characters += len(text)
        has_text = has_text or bool(text.strip())
        if characters > MAX_TEXT:
            raise PDFWordError("Extracted text exceeds the 1 million character processing limit.")
    return has_text


def place_page_image(paragraph, image: bytes, width: float, height: float) -> None:
    """Anchor a full-page image outside text flow to avoid extra blank Word pages."""
    from io import BytesIO

    from docx.oxml import OxmlElement
    from docx.shared import Pt

    picture = paragraph.add_run().add_picture(BytesIO(image), width=Pt(width), height=Pt(height))
    inline = picture._inline
    anchor = OxmlElement("wp:anchor")
    for key, value in {
        "distT": "0",
        "distB": "0",
        "distL": "0",
        "distR": "0",
        "simplePos": "0",
        "relativeHeight": "0",
        "behindDoc": "1",
        "locked": "0",
        "layoutInCell": "1",
        "allowOverlap": "1",
    }.items():
        anchor.set(key, value)
    simple = OxmlElement("wp:simplePos")
    simple.set("x", "0")
    simple.set("y", "0")
    anchor.append(simple)
    for axis in ("H", "V"):
        position = OxmlElement("wp:position" + axis)
        position.set("relativeFrom", "page")
        offset = OxmlElement("wp:posOffset")
        offset.text = "0"
        position.append(offset)
        anchor.append(position)
    # Word's drawing schema requires the extent before wrapping and graphic data.
    anchor.append(inline.extent)
    anchor.append(OxmlElement("wp:wrapNone"))
    for child in list(inline):
        anchor.append(child)
    inline.getparent().replace(inline, anchor)


def appearance_docx(pdf, target: Path) -> None:
    from docx import Document
    from docx.enum.section import WD_ORIENT, WD_SECTION_START
    from docx.shared import Pt

    document = Document()
    total_images = 0
    for index, page in enumerate(pdf):
        width, height = page.rect.width, page.rect.height
        if width < 36 or height < 36 or width > 1584 or height > 1584:
            raise PDFWordError("This PDF has a page size that Word cannot represent reliably.")
        if width * height * 4 > MAX_PAGE_PIXELS:
            raise PDFWordError("A page exceeds the safe image rendering limit.")
        section = (
            document.sections[0] if not index else document.add_section(WD_SECTION_START.NEW_PAGE)
        )
        section.page_width, section.page_height = Pt(width), Pt(height)
        section.orientation = WD_ORIENT.LANDSCAPE if width > height else WD_ORIENT.PORTRAIT
        section.top_margin = section.bottom_margin = Pt(0)
        section.left_margin = section.right_margin = Pt(0)
        section.header_distance = section.footer_distance = Pt(0)
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = Pt(1)
        # RGB on white gives stable rendering of transparent PDF backgrounds.
        pixels = page.get_pixmap(dpi=144, alpha=False)
        image = pixels.tobytes("png")
        total_images += len(image)
        if total_images > MAX_IMAGE_BYTES:
            raise PDFWordError(
                "Rendered pages exceed the 48 MB image budget. Split this PDF first."
            )
        place_page_image(paragraph, image, width, height)
        del pixels, image
    document.save(target)


def normalize_column_breaks(target: Path) -> None:
    """Use a column break, rather than an identical new section, between columns.

    Some Word readers paginate nextColumn sections onto a new page. Only merge
    sections whose page/column/header settings match, preserving real changes.
    """
    from copy import deepcopy

    from docx import Document
    from docx.enum.section import WD_SECTION_START
    from docx.enum.text import WD_BREAK
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    from lxml import etree

    def settings(section):
        element = deepcopy(section._sectPr)
        element.attrib.clear()  # rsid attributes describe editing history, not layout.
        for child in element.findall(qn("w:type")):
            element.remove(child)
        return etree.tostring(element, method="c14n")

    document = Document(target)
    index = 1
    changed = False
    while index < len(document.sections):
        previous, current = document.sections[index - 1], document.sections[index]
        if current.start_type == WD_SECTION_START.NEW_COLUMN and settings(previous) == settings(
            current
        ):
            current.start_type = previous.start_type
            paragraph = previous._sectPr.getparent().getparent()
            previous._sectPr.getparent().remove(previous._sectPr)
            Paragraph(paragraph, document._body).add_run().add_break(WD_BREAK.COLUMN)
            changed = True
            index = max(1, index - 1)
        else:
            index += 1
    if changed:
        document.save(target)


def convert(source: Path, target: Path, mode: str) -> None:
    import pymupdf

    with pymupdf.open(source) as document:
        has_text = inspect_pdf(document, mode)
        if mode == "appearance":
            appearance_docx(document, target)
            return
        if mode == "ocr" or mode == "editable" and not has_text:
            from app.ocr import ocr_docx

            ocr_docx(document, target)
            return
        if mode == "precise":
            from app.pdf_precise import precise_docx

            precise_docx(document, target)
            return
    from pdf2docx import Converter

    converter = Converter(str(source))
    try:
        converter.convert(
            str(target),
            multi_processing=False,
            ignore_page_error=False,
            raw_exceptions=True,
            line_break_width_ratio=1.0,
        )
    finally:
        converter.close()
    normalize_column_breaks(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("precise", "editable", "ocr", "appearance"), default="precise"
    )
    args = parser.parse_args()
    logging.getLogger().setLevel(logging.ERROR)
    try:
        convert(args.source, args.target, args.mode)
    except PDFWordError as error:
        result = {"ok": False, "message": str(error)}
    except Exception as error:  # noqa: BLE001 - the worker must write safe status for native-parser errors.
        logger.error("PDF-to-Word failed: %s", type(error).__name__)
        result = {
            "ok": False,
            "message": (
                "This PDF could not be converted in the selected mode. "
                "Try Exact appearance for complex layouts, or check that the PDF is not damaged."
            ),
        }
    else:
        result = {"ok": True}
    args.status.write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
