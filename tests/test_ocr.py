import io
import os
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest
from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from app import conversion, ocr
from app.conversion import ConversionError, convert_bytes, office_convert
from app.formats import office_available, registry
from app.main import app
from app.pdf_word import PDFWordError


@pytest.fixture(scope="module")
def scan_pdf():
    image = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("DejaVuSans.ttf", 40)
    for index, line in enumerate(
        [
            "SCANNED APPLICATION FORM",
            "Applicant: Alex Example",
            "Reference: ABC123456",
            "This text should be editable in Word.",
        ]
    ):
        draw.text((100, 150 + index * 100), line, font=font, fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PDF", resolution=200)
    return stream.getvalue()


@pytest.fixture(scope="module")
def recognized_word(scan_pdf):
    if not ocr.ocr_available():
        pytest.skip("Tesseract with English language data is required")
    return convert_bytes(scan_pdf, "pdf", "docx", "ocr")


def test_real_scan_becomes_editable_word_text(scan_pdf, recognized_word):
    with pymupdf.open(stream=scan_pdf, filetype="pdf") as pdf:
        assert not pdf[0].get_text().strip()
    word = Document(io.BytesIO(recognized_word))
    text = "\n".join(paragraph.text for paragraph in word.paragraphs)
    assert "SCANNED APPLICATION FORM" in text
    assert "Applicant: Alex Example" in text
    assert "Reference: ABC123456" in text
    assert "This text should be editable in Word." in text
    assert not word.inline_shapes
    assert not word._element.xpath(".//wp:anchor")


@pytest.mark.skipif(
    not office_available(), reason="LibreOffice is required for independent rendering"
)
def test_ocr_word_can_be_rendered_and_edited_normally(recognized_word, tmp_path):
    word = Document(io.BytesIO(recognized_word))
    word.paragraphs[1].text = "Applicant: Changed Name"
    source, target = tmp_path / "ocr.docx", tmp_path / "rendered.pdf"
    word.save(source)
    office_convert(source, target, "pdf")
    with pymupdf.open(target) as pdf:
        assert len(pdf) == 1
        assert "Applicant: Changed Name" in pdf[0].get_text()
        assert "Alex Example" not in pdf[0].get_text()


@pytest.mark.skipif(not ocr.ocr_available(), reason="Tesseract is required")
def test_editable_mode_automatically_recognizes_fully_scanned_pdf(scan_pdf):
    word = Document(io.BytesIO(convert_bytes(scan_pdf, "pdf", "docx", "editable")))
    assert "Applicant: Alex Example" in "\n".join(paragraph.text for paragraph in word.paragraphs)


@pytest.mark.skipif(not ocr.ocr_available(), reason="Tesseract is required")
def test_ocr_handles_mixed_digital_and_scanned_pages_with_digital_header(scan_pdf):
    with pymupdf.open() as mixed, pymupdf.open(stream=scan_pdf, filetype="pdf") as scan:
        mixed.new_page().insert_text((50, 70), "Digital introduction - keep this text.")
        mixed.insert_pdf(scan)
        mixed[1].insert_text((50, 20), "Digital header on scanned page")
        result = convert_bytes(mixed.tobytes(), "pdf", "docx", "ocr")
    word = Document(io.BytesIO(result))
    text = "\n".join(p.text for p in word.paragraphs)
    assert "Digital introduction - keep this text." in text
    assert "Applicant: Alex Example" in text
    assert text.count("Applicant: Alex Example") == 1
    assert len(word.sections) == 2


def test_ocr_unavailable_is_honest_and_keeps_appearance_mode(scan_pdf, monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "installed_languages", lambda: ())
    capabilities = registry()
    assert not capabilities["ocr_available"]
    assert "ocr" not in [mode["value"] for mode in capabilities["pdf_word_modes"]]
    with (
        pymupdf.open(stream=scan_pdf, filetype="pdf") as pdf,
        pytest.raises(PDFWordError, match="Install Tesseract"),
    ):
        ocr.ocr_docx(pdf, tmp_path / "missing.docx")
    result = convert_bytes(scan_pdf, "pdf", "docx", "appearance")
    assert len(Document(io.BytesIO(result))._element.xpath(".//wp:anchor")) == 1


def test_ocr_rejects_excessive_scanned_pages_and_pixel_counts(scan_pdf):
    with pymupdf.open() as many, pymupdf.open(stream=scan_pdf, filetype="pdf") as scan:
        for _ in range(11):
            many.insert_pdf(scan)
        with pytest.raises(ConversionError, match="10 scanned pages"):
            convert_bytes(many.tobytes(), "pdf", "docx", "ocr")
    with pymupdf.open() as huge:
        page = huge.new_page(width=1200, height=1200)
        page.draw_rect((50, 50, 100, 100))
        with pytest.raises(ConversionError, match="8 megapixel"):
            convert_bytes(huge.tobytes(), "pdf", "docx", "ocr")


def test_ocr_page_timeout_cleans_rendered_images(scan_pdf, tmp_path, monkeypatch):
    def timeout(*_, **__):
        raise subprocess.TimeoutExpired("tesseract", 20)

    monkeypatch.setattr(ocr.subprocess, "run", timeout)
    with (
        pymupdf.open(stream=scan_pdf, filetype="pdf") as pdf,
        pytest.raises(PDFWordError, match="took too long"),
    ):
        ocr.recognize_page(pdf[0], tmp_path, 0)
    assert not list(tmp_path.iterdir())


def test_uncertain_words_are_highlighted_for_review(scan_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(
        ocr,
        "recognize_page",
        lambda *args: [[{"text": "UncertainName", "confidence": 25, "height": 9}]],
    )
    output = tmp_path / "review.docx"
    with pymupdf.open(stream=scan_pdf, filetype="pdf") as pdf:
        ocr.ocr_docx(pdf, output)
    word = Document(output)
    run = word.paragraphs[0].runs[0]
    assert run.text == "UncertainName"
    assert run.font.highlight_color == WD_COLOR_INDEX.YELLOW


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
def test_worker_timeout_stops_ocr_child_process(tmp_path, monkeypatch):
    marker = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(60)"
    )
    original = subprocess.Popen

    def slow_worker(*args, **kwargs):
        return original([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(conversion.subprocess, "Popen", slow_worker)
    monkeypatch.setattr(conversion, "PDF_WORD_TIMEOUT", 1)
    with pytest.raises(ConversionError, match="timed out"):
        conversion.pdf_word_convert(tmp_path / "source.pdf", tmp_path / "out.docx", "ocr")
    pid = int(marker.read_text())
    state = Path(f"/proc/{pid}/stat")
    assert not state.exists() or state.read_text().split()[2] == "Z"


@pytest.mark.skipif(not ocr.ocr_available(), reason="Tesseract is required")
def test_api_advertises_and_returns_real_ocr_download(scan_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    with TestClient(app) as client:
        capabilities = client.get("/api/formats").json()
        assert capabilities["ocr_available"]
        assert "ocr" in [mode["value"] for mode in capabilities["pdf_word_modes"]]
        response = client.post(
            "/api/convert",
            files={"file": ("scan.pdf", scan_pdf)},
            data={"target": "docx", "pdf_mode": "ocr"},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        word = Document(io.BytesIO(response.content))
        assert "Alex Example" in "\n".join(p.text for p in word.paragraphs)
        assert not list(tmp_path.iterdir())
