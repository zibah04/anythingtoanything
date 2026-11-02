"""Download real OCR output through the desktop/mobile web interface."""

import io
import os
import shutil
from pathlib import Path

from docx import Document
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8000")
ARTIFACTS = ROOT / "artifacts"


def run():
    image = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("DejaVuSans.ttf", 40)
    for index, line in enumerate(
        ["SCANNED DOCUMENT", "Applicant: Alex Example", "Reference: ABC123456"]
    ):
        draw.text((100, 150 + index * 100), line, font=font, fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PDF", resolution=200)
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
            {"name": "scan.pdf", "mimeType": "application/pdf", "buffer": stream.getvalue()}
        )
        mode = page.get_by_label("Word conversion mode for scan.pdf")
        mode.select_option("ocr")
        expect(page.locator(".file-note")).to_contain_text("printed English")
        expect(page.locator(".file-note")).to_contain_text("proofread")
        for selected in ["ocr", "editable"]:
            mode.select_option(selected)
            expect(page.locator(".download-button")).to_have_count(0)
            page.locator("#convert-button").click()
            expect(page.locator(".download-button")).to_have_count(1, timeout=90000)
            with page.expect_download() as result:
                page.get_by_role("link", name="Download scan.docx", exact=True).click()
            target = ARTIFACTS / f"browser-ocr-{selected}.docx"
            result.value.save_as(target)
            text = "\n".join(p.text for p in Document(target).paragraphs)
            assert "Alex Example" in text
            assert "ABC123456" in text
        mode.select_option("ocr")
        for width in [390, 320]:
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert mode.bounding_box()["height"] >= 44
            page.screenshot(path=str(ARTIFACTS / f"ocr-mobile-{width}.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print(
        "PASS: OCR selection, real editable downloads, automatic scan fallback, and mobile layout."
    )


if __name__ == "__main__":
    run()
