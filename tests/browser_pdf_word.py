"""Check PDF-to-Word mode selection, downloads, invalidation, and mobile controls."""

import io
import os
import shutil
from pathlib import Path

from docx import Document
from playwright.sync_api import expect, sync_playwright
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8000")
ARTIFACTS = ROOT / "artifacts"


def run():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(612, 792))
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(48, 720, "A Word document with layout")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(48, 680, "Keep this text editable.")
    pdf.save()
    ARTIFACTS.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("BROWSER_EXECUTABLE") or shutil.which("chromium"),
            headless=True,
            args=["--no-sandbox"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 960}, accept_downloads=True)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE_URL, wait_until="networkidle")
        expect(page.locator("#dropzone")).to_be_enabled()
        page.locator("#file-input").set_input_files(
            {
                "name": "layout.pdf",
                "mimeType": "application/pdf",
                "buffer": stream.getvalue(),
            }
        )
        mode = page.get_by_label("Word conversion mode for layout.pdf")
        expect(mode).to_have_value("editable")
        expect(page.locator(".file-note")).to_contain_text("editable text")
        page.locator("#convert-button").click()
        expect(page.locator(".download-button")).to_have_count(1, timeout=90000)
        with page.expect_download() as info:
            page.get_by_role("link", name="Download layout.docx", exact=True).click()
        info.value.save_as(ARTIFACTS / "browser-editable.docx")
        word = Document(ARTIFACTS / "browser-editable.docx")
        assert "Keep this text editable." in " ".join(p.text for p in word.paragraphs)
        mode.select_option("appearance")
        expect(page.locator(".download-button")).to_have_count(0)
        expect(page.locator(".file-note")).to_contain_text("not editable")
        page.locator("#convert-button").click()
        expect(page.locator(".download-button")).to_have_count(1, timeout=90000)
        with page.expect_download() as info:
            page.get_by_role("link", name="Download layout.docx", exact=True).click()
        info.value.save_as(ARTIFACTS / "browser-appearance.docx")
        word = Document(ARTIFACTS / "browser-appearance.docx")
        assert len(word._element.xpath(".//wp:anchor")) == 1
        for width in [390, 320]:
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert mode.bounding_box()["height"] >= 44
            page.screenshot(path=str(ARTIFACTS / f"pdf-word-mobile-{width}.png"), full_page=True)
        page.get_by_label("Output format for layout.pdf").select_option("txt")
        expect(mode).to_have_count(0)
        expect(page.locator(".file-note")).to_contain_text("Extracts text")
        assert not errors, errors
        browser.close()
    print(
        "PASS: editable/appearance Word modes, real DOCX downloads, result invalidation, accurate notices, and mobile controls."
    )


if __name__ == "__main__":
    run()
