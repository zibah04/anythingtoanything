"""End-to-end regression coverage for optional offline OCR routes."""

import io
import shutil

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from app.conversion import ConversionError, convert_bytes
from app.formats import IMAGES, target_map
from app.main import app
from app.ocr import MAX_OCR_PAGES, available

pytestmark = pytest.mark.skipif(
    not available(), reason="Tesseract must be installed for OCR integration tests"
)


@pytest.fixture
def scanned_image():
    image = Image.new("RGB", (900, 230), "white")
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not shutil.which("tesseract"):
        pytest.skip("Tesseract unavailable")
    try:
        font = ImageFont.truetype(font_path, 40)
    except OSError:
        pytest.skip("DejaVu font unavailable")
    ImageDraw.Draw(image).text((35, 80), "HELLO OCR WORLD", font=font, fill="black")
    return image


def test_image_ocr_is_advertised_and_returns_text(scanned_image):
    assert all("txt" in target_map()[extension] for extension in IMAGES)
    output = io.BytesIO()
    scanned_image.save(output, "PNG")
    result = convert_bytes(output.getvalue(), "png", "txt").decode("utf-8")
    assert "HELLO OCR WORLD" in result.upper()


def test_scanned_pdf_ocr_returns_text(scanned_image):
    output = io.BytesIO()
    scanned_image.save(output, "PDF")
    result = convert_bytes(output.getvalue(), "pdf", "txt").decode("utf-8")
    assert "HELLO OCR WORLD" in result.upper()


def test_mixed_native_and_scanned_pdf(scanned_image):
    import pymupdf

    png = io.BytesIO()
    scanned_image.save(png, "PNG")
    with pymupdf.open() as pdf:
        plain = pdf.new_page()
        plain.insert_text((40, 80), "Native text is preserved")
        scan = pdf.new_page(width=900, height=230)
        scan.insert_image(scan.rect, stream=png.getvalue())
        result = convert_bytes(pdf.tobytes(), "pdf", "txt").decode("utf-8")
    assert "Native text is preserved" in result
    assert "HELLO OCR WORLD" in result.upper()


def test_ocr_is_available_from_http_api(scanned_image):
    data = io.BytesIO()
    scanned_image.save(data, "PNG")
    with TestClient(app) as client:
        formats = client.get("/api/formats").json()
        assert formats["ocr_available"] is True
        response = client.post(
            "/api/convert",
            files={"file": ("picture.png", data.getvalue())},
            data={"target": "txt"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "HELLO OCR WORLD" in response.text.upper()


def test_blank_scan_has_actionable_error():
    stream = io.BytesIO()
    Image.new("RGB", (800, 250), "white").save(stream, "PNG")
    with pytest.raises(ConversionError, match="OCR"):
        convert_bytes(stream.getvalue(), "png", "txt")


def test_ocr_page_limit_is_bounded(scanned_image):
    import pymupdf

    png = io.BytesIO()
    scanned_image.save(png, "PNG")
    with pymupdf.open() as pdf:
        # Blank pages count toward OCR page budget, but avoid OCR work by
        # making the limit test use a monkeypatched Tesseract response.
        for _ in range(MAX_OCR_PAGES + 1):
            page = pdf.new_page(width=900, height=230)
            page.insert_image(page.rect, stream=png.getvalue())
        payload = pdf.tobytes()
    from unittest.mock import patch

    with patch("app.ocr._recognize_png", return_value="HELLO OCR WORLD") as recognize:
        with pytest.raises(ConversionError, match="Split the PDF"):
            convert_bytes(payload, "pdf", "txt")
        assert recognize.call_count == MAX_OCR_PAGES
