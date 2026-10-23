import io
import json
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import pytest
from docx import Document
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from PIL import Image
from pptx import Presentation
from pypdf import PdfReader

from app.conversion import ConversionError, convert_bytes, office_convert, validate_archive
from app.formats import IMAGES, office_available, target_map
from app.main import MAX_UPLOAD, app


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    data = {
        "txt": b"Hello conversion\nA fresh format.",
        "md": b"# Hello conversion\n\nA **fresh** format.\n",
        "html": b"<!doctype html><html><body><h1>Hello conversion</h1><p>A fresh format.</p></body></html>",
    }
    document = Document()
    document.add_heading("Hello conversion", 0)
    document.add_paragraph("A fresh format.")
    stream = io.BytesIO()
    document.save(stream)
    data["docx"] = stream.getvalue()
    for extension in ("odt", "rtf", "epub", "pdf"):
        data[extension] = convert_bytes(data["md"], "md", extension)
    data["csv"] = b'name,note\r\nAlice,"Hello conversion, again"\r\nBob,"Two\nlines"\r\n'
    data["tsv"] = b"name\tnote\nAlice\tHello conversion\n"
    data["json"] = b'[{"name":"Alice","note":"Hello conversion"}]'
    workbook = Workbook()
    workbook.active.append(["name", "note"])
    workbook.active.append(["Alice", "Hello conversion"])
    other = workbook.create_sheet("Second")
    other.append(["Other sheet"])
    stream = io.BytesIO()
    workbook.save(stream)
    data["xlsx"] = stream.getvalue()
    data["ods"] = convert_bytes(data["xlsx"], "xlsx", "ods")
    for extension in IMAGES:
        stream = io.BytesIO()
        image = Image.new("RGB", (24, 18), (55, 115, 60))
        image.save(stream, format={"jpg": "JPEG", "tiff": "TIFF"}.get(extension, extension.upper()))
        data[extension] = stream.getvalue()
    slides = Presentation()
    slide = slides.slides.add_slide(slides.slide_layouts[1])
    slide.shapes.title.text = "Hello conversion"
    slide.placeholders[1].text = "A fresh format."
    stream = io.BytesIO()
    slides.save(stream)
    data["pptx"] = stream.getvalue()
    if office_available():
        root = tmp_path_factory.mktemp("legacy")
        for original, extension in [
            ("docx", "doc"),
            ("xlsx", "xls"),
            ("pptx", "ppt"),
            ("pptx", "odp"),
        ]:
            source, target = root / f"sample.{original}", root / f"sample.{extension}"
            source.write_bytes(data[original])
            office_convert(source, target, extension)
            data[extension] = target.read_bytes()
    return data


CASES = [(source, target) for source, targets in target_map().items() for target in targets]


@pytest.mark.parametrize("source,target", CASES)
def test_all_advertised_conversion_routes(source, target, samples):
    result = convert_bytes(samples[source], source, target)
    assert result
    if target == "pdf":
        pdf = PdfReader(io.BytesIO(result))
        assert len(pdf.pages) >= 1
        if source not in IMAGES:
            assert "Hello conversion" in " ".join(page.extract_text() or "" for page in pdf.pages)
    elif target in IMAGES:
        with Image.open(io.BytesIO(result)) as image:
            assert image.size == (24, 18)
            image.load()
    elif target == "docx":
        document = Document(io.BytesIO(result))
        assert "Hello conversion" in " ".join(paragraph.text for paragraph in document.paragraphs)
    elif target == "xlsx":
        workbook = load_workbook(io.BytesIO(result))
        assert "Hello conversion" in str(list(workbook.active.values))
    elif target == "json":
        assert "Hello conversion" in str(json.loads(result))
    elif target == "pptx":
        assert Presentation(io.BytesIO(result)).slides
    elif target in {"odt", "ods", "epub", "odp"}:
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            assert archive.testzip() is None
    else:
        assert "Hello conversion" in result.decode("utf-8")


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_api_returns_download_and_private_headers(client, samples):
    response = client.post(
        "/api/convert", files={"file": ("Résumé.docx", samples["docx"])}, data={"target": "pdf"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9.pdf" in response.headers["content-disposition"]
    assert PdfReader(io.BytesIO(response.content)).pages


def test_api_reports_real_capabilities_and_serves_ui(client):
    response = client.get("/api/formats")
    assert response.status_code == 200
    formats = response.json()["formats"]
    assert len(formats) >= 20
    png = next(item for item in formats if item["extension"] == "png")
    assert (
        next(item for item in png["targets"] if item["extension"] == "jpg")["engine"] == "browser"
    )
    assert client.get("/api/health").json()["status"] == "ok"
    assert "Every file," in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


@pytest.mark.parametrize("entry", ["/", "/index.html", "/static/", "/static/index.html"])
def test_frontend_entry_points_resolve_styles_and_modules(client, entry):
    class Assets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = []

        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if tag == "link" and attributes.get("rel") == "stylesheet":
                self.paths.append((attributes["href"], "text/css"))
            elif tag == "script" and attributes.get("src"):
                self.paths.append((attributes["src"], "javascript"))

    response = client.get(entry)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    parser = Assets()
    parser.feed(response.text)
    assert len(parser.paths) == 2
    for path, content_type in parser.paths:
        asset = client.get(urljoin(entry, path))
        assert asset.status_code == 200
        assert content_type in asset.headers["content-type"]
        assert asset.headers["cache-control"] == "no-cache"
    assert client.get("/paths.js").status_code == 200
    assert client.get("/local.js").status_code == 200
    assert client.get("/missing.css").status_code == 404


@pytest.mark.parametrize(
    "filename,data,target,status,message",
    [
        ("empty.txt", b"", "pdf", 400, "empty"),
        ("fake.docx", b"not a Word file", "pdf", 422, "could not"),
        ("file.txt", b"hello", "xlsx", 422, "not supported"),
        ("file.exe", b"hello", "pdf", 422, "not supported"),
        ("file.json", b'{"not":"an array"}', "xlsx", 422, "array"),
        ("bad.csv", b"name,name\nAlice,Bob", "json", 422, "unique"),
        ("bad.csv", b'name,note\n"unterminated', "json", 422, "could not"),
        ("file.txt", b"\xff\x00\xaa", "pdf", 422, "UTF"),
    ],
)
def test_api_errors_are_actionable(client, filename, data, target, status, message):
    response = client.post(
        "/api/convert", files={"file": (filename, data)}, data={"target": target}
    )
    assert response.status_code == status
    assert message.lower() in response.json()["detail"].lower()


def test_empty_scanned_pdf_explains_ocr(client):
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(stream, format="PDF")
    response = client.post(
        "/api/convert", files={"file": ("scan.pdf", stream.getvalue())}, data={"target": "txt"}
    )
    assert response.status_code == 422
    assert "OCR" in response.json()["detail"]


def test_size_limit(client):
    response = client.post(
        "/api/convert", headers={"content-length": str(MAX_UPLOAD + 1024 * 1024 + 1)}
    )
    assert response.status_code == 413


def test_formula_injection_is_not_activated():
    source = b"name,value\nAlice,=1+1\n"
    result = convert_bytes(source, "csv", "xlsx")
    workbook = load_workbook(io.BytesIO(result))
    assert workbook.active["B2"].data_type == "s"
    assert workbook.active["B2"].value == "=1+1"
    result = convert_bytes(b'[{"value":"=1+1"}]', "json", "csv")
    assert "'=1+1" in result.decode()


def test_multiple_sheets_survive_xlsx_ods_round_trip(samples):
    result = convert_bytes(samples["ods"], "ods", "xlsx")
    workbook = load_workbook(io.BytesIO(result))
    assert workbook.sheetnames == ["Sheet", "Second"]
    assert workbook["Second"]["A1"].value == "Other sheet"


def test_temporary_documents_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    convert_bytes(b"Hello", "txt", "pdf")
    assert not list(tmp_path.iterdir())
    with pytest.raises(ConversionError):
        convert_bytes(b"broken", "docx", "pdf")
    assert not list(tmp_path.iterdir())


def test_archive_expansion_limit(tmp_path):
    source = Path(tmp_path) / "large.docx"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb", b"0" * (128 * 1024 * 1024 + 1))
    with pytest.raises(ConversionError, match="expands"):
        validate_archive(source)


def test_text_pdf_fallback_and_capabilities_without_office(samples, monkeypatch):
    monkeypatch.setattr("app.formats.office_available", lambda: False)
    monkeypatch.setattr("app.conversion.office_available", lambda: False)
    capabilities = target_map()
    assert not {"doc", "xls", "ppt", "odp"} & capabilities.keys()
    assert "odp" not in capabilities["pptx"]
    for source in ("docx", "pptx"):
        result = convert_bytes(samples[source], source, "pdf")
        assert "Hello conversion" in PdfReader(io.BytesIO(result)).pages[0].extract_text()


def test_embedded_word_image_is_self_contained_in_html():
    document = Document()
    document.add_paragraph("Image example")
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "green").save(image, format="PNG")
    image.seek(0)
    document.add_picture(image)
    word = io.BytesIO()
    document.save(word)
    result = convert_bytes(word.getvalue(), "docx", "html").decode()
    assert "data:image/png;base64," in result


def test_streaming_body_limit_without_content_length(client):
    def chunks():
        for _ in range(27):
            yield b"a" * (1024 * 1024)

    response = client.post(
        "/api/convert",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=test"},
    )
    assert response.status_code == 413


def test_xlsx_without_dimension_metadata():
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Streaming")
    sheet.append(["name", "note"])
    sheet.append(["Alice", "Written without dimensions"])
    source = io.BytesIO()
    workbook.save(source)
    result = convert_bytes(source.getvalue(), "xlsx", "csv")
    assert "Alice,Written without dimensions" in result.decode()


def test_ods_round_trip_preserves_numbers_booleans_dates_and_numeric_strings():
    from datetime import datetime

    workbook = Workbook()
    values = [42, 3.25, True, datetime(2026, 10, 23), "0042"]
    workbook.active.append(values)
    source = io.BytesIO()
    workbook.save(source)
    ods = convert_bytes(source.getvalue(), "xlsx", "ods")
    result = convert_bytes(ods, "ods", "xlsx")
    restored = load_workbook(io.BytesIO(result))
    assert next(iter(restored.active.values)) == tuple(values)
