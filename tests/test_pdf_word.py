import io
import zipfile

import pymupdf
import pytest
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageFilter, ImageStat
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

from app.conversion import ConversionError, convert_bytes, office_convert
from app.formats import conversion_note, office_available
from app.main import app


@pytest.fixture(scope="module")
def layout_pdf():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(612, 792))
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(48, 730, "Layout preservation check")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(48, 690, "Keep the table, image, fonts, and page arrangement.")
    table = Table(
        [["Item", "Qty", "Price"], ["Pencils", "3", "5.00"], ["Notebooks", "2", "12.00"]],
        colWidths=[200, 80, 80],
        rowHeights=24,
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ]
        )
    )
    table.wrapOn(pdf, 500, 500)
    table.drawOn(pdf, 48, 500)
    pdf.drawImage(ImageReader(Image.new("RGB", (100, 60), "green")), 420, 590, width=100, height=60)
    pdf.showPage()
    pdf.setPageSize((792, 612))
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(48, 560, "Two-column article")
    for index in range(10):
        pdf.setFont("Helvetica", 11)
        pdf.drawString(
            48, 510 - index * 18, f"Left column line {index + 1}. Keep reading in order."
        )
        pdf.drawString(420, 510 - index * 18, f"Right column line {index + 1}. Then continue here.")
    pdf.save()
    return stream.getvalue()


@pytest.fixture(scope="module")
def editable_word(layout_pdf):
    return convert_bytes(layout_pdf, "pdf", "docx", "editable")


@pytest.fixture(scope="module")
def appearance_word(layout_pdf):
    return convert_bytes(layout_pdf, "pdf", "docx", "appearance")


def test_editable_word_preserves_tables_images_styles_columns_and_page_sizes(editable_word):
    word = Document(io.BytesIO(editable_word))
    table = word.tables[0]
    assert [[cell.text for cell in row.cells] for row in table.rows] == [
        ["Item", "Qty", "Price"],
        ["Pencils", "3", "5.00"],
        ["Notebooks", "2", "12.00"],
    ]
    assert len(word.inline_shapes) == 1
    assert any(
        run.bold and run.font.size and run.font.size.pt == 24
        for paragraph in word.paragraphs
        for run in paragraph.runs
    )
    text = "\n".join(paragraph.text for paragraph in word.paragraphs)
    assert text.index("Left column line 10.") < text.index("Right column line 1.")
    assert (word.sections[0].page_width.pt, word.sections[0].page_height.pt) == (612, 792)
    assert any(
        section.page_width.pt == 792 and section.page_height.pt == 612 for section in word.sections
    )
    assert word._element.xpath('.//w:cols[@w:num="2"]')
    assert word._element.xpath('.//w:br[@w:type="column"]')


def test_appearance_word_keeps_pages_as_anchored_images(appearance_word):
    word = Document(io.BytesIO(appearance_word))
    assert len(word.sections) == 2
    assert len(word._element.xpath(".//wp:anchor")) == 2
    assert not "".join(paragraph.text for paragraph in word.paragraphs).strip()
    with zipfile.ZipFile(io.BytesIO(appearance_word)) as archive:
        assert len([name for name in archive.namelist() if name.startswith("word/media/")]) == 2


@pytest.mark.skipif(
    not office_available(), reason="LibreOffice is required to independently render Word output"
)
def test_editable_columns_do_not_add_a_page(editable_word, tmp_path):
    source, target = tmp_path / "editable.docx", tmp_path / "roundtrip.pdf"
    source.write_bytes(editable_word)
    office_convert(source, target, "pdf")
    reader = PdfReader(target)
    assert len(reader.pages) == 2
    text = " ".join(reader.pages[1].extract_text().split())
    assert "Left column line 10" in text
    assert "Right column line 10" in text


@pytest.mark.skipif(
    not office_available(), reason="LibreOffice is required to independently render Word output"
)
def test_appearance_preserves_page_geometry_and_visible_content(
    layout_pdf, appearance_word, tmp_path
):
    source, target = tmp_path / "appearance.docx", tmp_path / "roundtrip.pdf"
    source.write_bytes(appearance_word)
    office_convert(source, target, "pdf")
    with (
        pymupdf.open(stream=layout_pdf, filetype="pdf") as original,
        pymupdf.open(target) as rendered,
    ):
        assert len(rendered) == len(original) == 2
        for index in range(len(original)):
            assert rendered[index].rect == original[index].rect
            expected = original[index].get_pixmap(dpi=72).pil_image()
            actual = rendered[index].get_pixmap(dpi=72).pil_image()
            ink = expected.convert("L").point(lambda value: 255 if value < 230 else 0)
            # A one-pixel blur tolerates PDF image resampling/antialiasing, while
            # comparing only ink keeps blank pages or displaced content from passing.
            difference = ImageStat.Stat(
                ImageChops.difference(
                    expected.filter(ImageFilter.GaussianBlur(1)),
                    actual.filter(ImageFilter.GaussianBlur(1)),
                ),
                mask=ink,
            )
            assert sum(difference.mean) / 3 < 20


def test_scanned_pdf_appearance_succeeds_without_claiming_editable_text():
    scan = io.BytesIO()
    Image.new("RGB", (200, 200), "navy").save(scan, format="PDF")
    result = convert_bytes(scan.getvalue(), "pdf", "docx", "appearance")
    assert len(Document(io.BytesIO(result))._element.xpath(".//wp:anchor")) == 1
    with pytest.raises(ConversionError, match="OCR"):
        convert_bytes(scan.getvalue(), "pdf", "docx", "editable")
    precise = Document(io.BytesIO(convert_bytes(scan.getvalue(), "pdf", "docx")))
    assert len(precise._element.xpath(".//wp:anchor")) == 1
    assert not precise._element.xpath(".//w:txbxContent")


def test_pdf_word_rejects_passwords_and_excessive_pages(layout_pdf):
    writer = PdfWriter()
    writer.append(io.BytesIO(layout_pdf))
    writer.encrypt("protected")
    locked = io.BytesIO()
    writer.write(locked)
    with pytest.raises(ConversionError, match="Unlock"):
        convert_bytes(locked.getvalue(), "pdf", "docx")
    writer = PdfWriter()
    for _ in range(51):
        writer.add_blank_page(width=612, height=792)
    many_pages = io.BytesIO()
    writer.write(many_pages)
    with pytest.raises(ConversionError, match="50 pages"):
        convert_bytes(many_pages.getvalue(), "pdf", "docx", "appearance")


def test_pdf_word_api_modes_and_errors(layout_pdf):
    with TestClient(app) as client:
        pdf = next(
            item
            for item in client.get("/api/formats").json()["formats"]
            if item["extension"] == "pdf"
        )
        assert (
            "editable text boxes"
            in next(item for item in pdf["targets"] if item["extension"] == "docx")["note"]
        )
        response = client.post(
            "/api/convert",
            files={"file": ("report.pdf", layout_pdf)},
            data={"target": "docx", "pdf_mode": "appearance"},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert len(Document(io.BytesIO(response.content))._element.xpath(".//wp:anchor")) == 2
        response = client.post(
            "/api/convert",
            files={"file": ("report.pdf", layout_pdf)},
            data={"target": "docx", "pdf_mode": "unknown"},
        )
        assert response.status_code == 422
        assert "Choose Precise layout" in response.json()["detail"]
    assert "Extracts text" in conversion_note("pdf", "txt")


def test_pdf_word_cleans_temporary_files_on_success_and_failure(layout_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    convert_bytes(layout_pdf, "pdf", "docx")
    assert not list(tmp_path.iterdir())
    with pytest.raises(ConversionError):
        convert_bytes(b"broken PDF", "pdf", "docx")
    assert not list(tmp_path.iterdir())
