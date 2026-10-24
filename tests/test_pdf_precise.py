"""Regression coverage for printed forms, without retaining uploaded user data."""

import io
import uuid
import zipfile
from pathlib import Path

import pymupdf
import pytest
import reportlab
from docx import Document
from fastapi.testclient import TestClient
from fontTools.ttLib import TTFont
from PIL import Image, ImageChops, ImageFilter, ImageStat
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as ReportFont
from reportlab.pdfgen import canvas

from app.conversion import convert_bytes, office_convert
from app.formats import office_available
from app.main import app
from app.pdf_precise import Fonts


@pytest.fixture(scope="module")
def form_pdf():
    fonts = Path("/usr/share/fonts/truetype/dejavu")
    if not (fonts / "DejaVuSans.ttf").is_file():
        pytest.skip("Install fonts-dejavu-core for embedded-font layout regression tests")
    pdfmetrics.registerFont(ReportFont("FormRegular", str(fonts / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(ReportFont("FormBold", str(fonts / "DejaVuSans-Bold.ttf")))
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(612, 792))
    for index in range(2):
        if index:
            pdf.setPageSize((792, 612))
        pdf.setFont("FormBold", 21)
        pdf.drawString(50, 540 if index else 720, f"Registration form {index + 1}")
        pdf.setFont("FormRegular", 9.25)
        pdf.drawString(50, 24, f"Page {index + 1} of 2")
        pdf.setStrokeColorRGB(0, 0.35, 0.2)
        pdf.roundRect(45, 200, 510, 290, 6)
        pdf.drawImage(
            ImageReader(Image.new("RGB", (90, 110), (170, 195, 190))),
            55,
            350,
            width=90,
            height=110,
        )
        for x, heading, value in [
            (175, "APPLICANT NAME", "Alex Example"),
            (370, "REFERENCE NUMBER", "ABC_0123456789"),
        ]:
            pdf.setFont("FormBold", 7.5)
            pdf.drawString(x, 450, heading)
            pdf.setFont("FormRegular", 10.5)
            pdf.drawString(x, 432, value)
        pdf.setFont("FormRegular", 10.5)
        pdf.drawString(175, 390, "Confirm the details before printing.")
        tracked = pdf.beginText(175, 370)
        tracked.setFont("FormBold", 8.25)
        tracked.setCharSpace(0.4)
        tracked.textOut("CONFIRMED / SUBMITTED")
        pdf.drawText(tracked)
        justified = pdf.beginText(55, 240)
        justified.setFont("FormRegular", 10)
        justified.setWordSpace(3)
        justified.textOut("Keep these words in their original positions.")
        pdf.drawText(justified)
        pdf.saveState()
        pdf.translate(565, 250)
        pdf.rotate(90)
        pdf.setFont("FormBold", 9)
        pdf.drawString(0, 0, "Rotated artwork")
        pdf.restoreState()
        pdf.showPage()
    pdf.save()
    # Printed web pages can contain drawable Type 3 fonts. Their geometry must
    # survive even though there is no TrueType font to embed in editable Word.
    with pymupdf.open(stream=stream.getvalue(), filetype="pdf") as document:
        glyph = document.get_new_xref()
        document.update_object(glyph, "<< >>")
        document.update_stream(glyph, b"600 0 0 0 600 700 d1 0 0 m 300 700 l 600 0 l h f")
        font = document.get_new_xref()
        document.update_object(
            font,
            "<< /Type /Font /Subtype /Type3 /FontBBox [0 0 600 700] "
            "/FontMatrix [.001 0 0 .001 0 0] "
            f"/CharProcs << /A {glyph} 0 R >> "
            "/Encoding << /Type /Encoding /Differences [65 /A] >> "
            "/FirstChar 65 /LastChar 65 /Widths [600] /Resources << >> >>",
        )
        page = document[0]
        font_dict = int(document.xref_get_key(page.xref, "Resources/Font")[1].split()[0])
        document.xref_set_key(font_dict, "Artwork", f"{font} 0 R")
        contents = document.get_new_xref()
        document.update_object(contents, "<< >>")
        document.update_stream(
            contents,
            page.read_contents()
            + b"\nq .1 .4 .2 rg BT /Artwork 16 Tf 1 0 0 1 500 680 Tm (AAA) Tj ET Q\n",
        )
        page.set_contents(contents)
        return document.tobytes()


@pytest.fixture(scope="module")
def precise_word(form_pdf):
    return convert_bytes(form_pdf, "pdf", "docx")


def test_precise_is_default_and_keeps_editable_text_in_boxes(precise_word, form_pdf):
    word = Document(io.BytesIO(precise_word))
    assert len(word.sections) == 2
    assert len(word._element.xpath(".//wp:anchor")) == 2
    assert word._element.xpath(".//w:txbxContent")
    text = "".join(word._element.xpath(".//w:t/text()"))
    assert text.count("Alex Example") == 2
    assert "ABC_0123456789" in text
    assert "Rotated artwork" not in text  # Preserved in artwork, not misrotated text.
    assert "AAA" not in text  # Type 3 glyphs also stay in the artwork.
    assert [(s.page_width.pt, s.page_height.pt) for s in word.sections] == [(612, 792), (792, 612)]
    with TestClient(app) as client:
        modes = client.get("/api/formats").json()["pdf_word_modes"]
        assert modes[0]["value"] == "precise"
        assert "Unsupported text" in modes[0]["note"]
        response = client.post(
            "/api/convert",
            files={"file": ("form.pdf", form_pdf)},
            data={"target": "docx"},
        )
        assert response.status_code == 200
        assert Document(io.BytesIO(response.content))._element.xpath(".//w:txbxContent")


def test_original_subset_fonts_are_embedded_with_unique_families(precise_word):
    with zipfile.ZipFile(io.BytesIO(precise_word)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        fonts = [name for name in names if name.startswith("word/fonts/")]
        assert len(fonts) == 2
        from lxml import etree

        table = etree.fromstring(archive.read("word/fontTable.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        embedded = table.findall("w:font/w:embedRegular", ns)
        assert len(embedded) == 2
        for entry in embedded:
            key = uuid.UUID(entry.get("{" + ns["w"] + "}fontKey"))
            # Locate the package part using its relationship, not font order.
            rel_id = entry.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            rels = etree.fromstring(archive.read("word/_rels/fontTable.xml.rels"))
            target = next(r.get("Target") for r in rels if r.get("Id") == rel_id)
            data = bytearray(archive.read("word/" + target))
            for index in range(32):
                data[index] ^= key.bytes[::-1][index % 16]
            with TTFont(io.BytesIO(data)) as font:
                assert font["name"].getBestFamilyName().startswith("PDF ")
                assert ord("A") in font.getBestCmap()


@pytest.mark.parametrize("permissions", [2, 4, 256, 512])
def test_fonts_that_disallow_editable_subset_embedding_stay_as_artwork(permissions):
    path = Path(reportlab.__file__).parent / "fonts/Vera.ttf"
    with TTFont(path) as font:
        font["OS/2"].fsType = permissions
        stream = io.BytesIO()
        font.save(stream)

    class Source:
        def extract_font(self, _):
            return "Restricted", "ttf", "TrueType", stream.getvalue()

    document = Document()
    assert Fonts(document, Source()).embed(1) is None
    assert not any("/fonts/" in str(part.partname) for part in document.part.package.parts)


@pytest.mark.skipif(not office_available(), reason="LibreOffice is required to render Word output")
def test_precise_form_roundtrip_keeps_pages_text_positions_and_artwork(
    form_pdf, precise_word, tmp_path
):
    word, output = tmp_path / "form.docx", tmp_path / "rendered.pdf"
    word.write_bytes(precise_word)
    office_convert(word, output, "pdf")
    with pymupdf.open(stream=form_pdf, filetype="pdf") as source, pymupdf.open(output) as actual:
        assert len(actual) == len(source) == 2
        for index, page in enumerate(source):
            rendered = actual[index]
            assert rendered.rect == page.rect
            expected_words = page.get_text("words")
            actual_words = rendered.get_text("words")
            for expected in expected_words:
                if expected[4] in {"Rotated", "artwork", "AAA"}:
                    continue
                matches = [word for word in actual_words if word[4] == expected[4]]
                assert matches, expected[4]
                nearest = min(
                    matches,
                    key=lambda word: abs(word[0] - expected[0]) + abs(word[1] - expected[1]),
                )
                assert abs(nearest[0] - expected[0]) < 1
            spans = lambda p: [
                s
                for b in p.get_text("dict")["blocks"]
                if b["type"] == 0
                for line in b["lines"]
                for s in line["spans"]
            ]
            for expected in spans(page):
                if expected["text"] in {"Rotated artwork", "AAA"}:
                    continue
                matches = [s for s in spans(rendered) if s["text"] == expected["text"]]
                assert matches, expected["text"]
                # FontDescriptor ascent values can differ from actual TTF metrics;
                # baseline coordinates measure placement without that bbox artifact.
                assert abs(matches[0]["origin"][1] - expected["origin"][1]) < 0.25
            expected = page.get_pixmap().pil_image()
            image = rendered.get_pixmap().pil_image()
            ink = expected.convert("L").point(lambda value: 255 if value < 230 else 0)
            difference = ImageStat.Stat(
                ImageChops.difference(
                    expected.filter(ImageFilter.GaussianBlur(1)),
                    image.filter(ImageFilter.GaussianBlur(1)),
                ),
                mask=ink,
            )
            assert sum(difference.mean) / 3 < 20


def test_invisible_ocr_text_does_not_become_a_visible_text_box():
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 100), "Invisible OCR layer", render_mode=3)
        page.insert_text((50, 150), "Visible text")
        word = Document(io.BytesIO(convert_bytes(pdf.tobytes(), "pdf", "docx")))
        text = "".join(word._element.xpath(".//w:t/text()"))
        assert "Visible text" in text
        assert "Invisible OCR layer" not in text
