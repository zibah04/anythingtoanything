"""Exercise real local/server conversions, downloads, errors, dialogs, and mobile layout.

Run a server first, then: uv run --group browser python tests/browser_smoke.py
Uses system Chromium when present, otherwise Playwright's installed Chromium.
"""

import io
import json
import os
import shutil
from pathlib import Path

from docx import Document
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)
BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8000")


def run():
    document = Document()
    document.add_heading("Browser conversion test", 0)
    document.add_paragraph("A real document, a new format.")
    word = io.BytesIO()
    document.save(word)
    image = io.BytesIO()
    Image.new("RGBA", (30, 20), (0, 0, 0, 0)).save(image, format="PNG")
    pdf_scan = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(pdf_scan, format="PDF")

    with sync_playwright() as playwright:
        executable = os.environ.get("BROWSER_EXECUTABLE") or shutil.which("chromium")
        browser = playwright.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"]
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100}, accept_downloads=True
        )
        page = context.new_page()
        errors, uploads = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                uploads.append(request.url) if request.url.endswith("/api/convert") else None
            ),
        )
        page.goto(BASE_URL, wait_until="networkidle")
        expect(page.locator("#dropzone")).to_be_enabled()
        page.screenshot(path=str(ARTIFACTS / "desktop.png"), full_page=True)

        page.locator("#formats-nav").click()
        expect(page.locator("#formats-dialog")).to_be_visible()
        page.locator("#format-search").fill("PDF")
        expect(page.locator(".catalog-row")).to_have_count(1)
        page.keyboard.press("Escape")
        expect(page.locator("#formats-dialog")).not_to_be_visible()

        page.locator("#file-input").set_input_files(
            [
                {
                    "name": "people.csv",
                    "mimeType": "text/csv",
                    "buffer": b'name,note\nAlice,"Hello, world"\n',
                },
                {"name": "transparent.png", "mimeType": "image/png", "buffer": image.getvalue()},
                {
                    "name": "report.docx",
                    "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "buffer": word.getvalue(),
                },
            ]
        )
        expect(page.locator(".file-row")).to_have_count(3)
        page.get_by_label("Output format for people.csv").select_option("json")
        expect(page.locator(".route-badge", has_text="On device")).to_have_count(2)
        page.locator("#convert-button").click()
        expect(page.locator(".download-button")).to_have_count(3, timeout=90000)
        assert len(uploads) == 1, f"Only Word should be uploaded; saw {len(uploads)} uploads"
        for filename in ["people.json", "transparent.jpg", "report.pdf"]:
            with page.expect_download() as download_info:
                page.get_by_role("link", name=f"Download {filename}", exact=True).click()
            download = download_info.value
            assert download.suggested_filename == filename
            target = ARTIFACTS / filename
            download.save_as(target)
        assert json.loads((ARTIFACTS / "people.json").read_text()) == [
            {"name": "Alice", "note": "Hello, world"}
        ]
        with Image.open(ARTIFACTS / "transparent.jpg") as converted:
            assert converted.size == (30, 20)
            assert min(converted.getpixel((0, 0))) >= 250, "Transparency should become white"
        assert (
            "Browser conversion test" in PdfReader(ARTIFACTS / "report.pdf").pages[0].extract_text()
        )
        page.screenshot(path=str(ARTIFACTS / "converted.png"), full_page=True)

        page.locator("#clear-files").click()
        expect(page.locator(".file-row")).to_have_count(0)
        page.locator("#file-input").set_input_files(
            {"name": "scanned.pdf", "mimeType": "application/pdf", "buffer": pdf_scan.getvalue()}
        )
        page.get_by_label("Output format for scanned.pdf").select_option("txt")
        page.locator("#convert-button").click()
        expect(page.locator(".file-status.error")).to_contain_text("OCR", timeout=30000)
        expect(page.get_by_role("button", name="Retry", exact=True)).to_be_enabled()
        page.locator("#clear-files").click()

        page.locator("#file-input").set_input_files(
            {"name": "bad.exe", "mimeType": "application/octet-stream", "buffer": b"test"}
        )
        expect(page.locator("#toast")).to_contain_text("unsupported format")
        expect(page.locator(".file-row")).to_have_count(0)
        page.locator("#privacy-button").click()
        expect(page.locator("#privacy-dialog")).to_be_visible()
        page.keyboard.press("Escape")

        for width in [390, 320]:
            page.set_viewport_size({"width": width, "height": 844})
            page.reload(wait_until="networkidle")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                f"Horizontal overflow at {width}px"
            )
            page.locator("#file-input").set_input_files(
                {
                    "name": "mobile.csv",
                    "mimeType": "text/csv",
                    "buffer": b"name,note\nAlice,Mobile\n",
                }
            )
            page.get_by_label("Output format for mobile.csv").select_option("json")
            page.locator("#convert-button").click()
            expect(page.locator(".download-button")).to_have_count(1)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(ARTIFACTS / f"mobile-{width}.png"), full_page=True)
        assert not errors, f"JavaScript errors: {errors}"
        print(
            "PASS: mixed batch, local-only conversions, server PDF, downloads, errors, dialogs, and 390/320px layouts."
        )
        browser.close()


if __name__ == "__main__":
    run()
