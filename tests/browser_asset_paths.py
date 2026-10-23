"""Regression check for unstyled directory/proxy previews and oversized icons."""

import os
import shutil
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ARTIFACTS = ROOT / "artifacts"


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT.parent), **kwargs)

    def log_message(self, *_):
        pass

    def proxy(self):
        path = self.path.removeprefix("/preview/converter")
        content = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        request = urllib.request.Request(
            BASE_URL + path,
            data=content if content else None,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "")},
        )
        try:
            response = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            self.send_response(response.status)
            for name in ["Content-Type", "Content-Disposition", "Cache-Control"]:
                if name in response.headers:
                    self.send_header(name, response.headers[name])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/preview/converter/"):
            self.proxy()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/preview/converter/"):
            self.proxy()
        else:
            self.send_error(404)


def run():
    ARTIFACTS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), PreviewHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    preview = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("BROWSER_EXECUTABLE") or shutil.which("chromium"),
                headless=True,
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1280, "height": 960})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for name, url, has_api in [
                ("app", BASE_URL + "/", True),
                ("static-entry", BASE_URL + "/static/index.html", True),
                ("proxy", preview + "/preview/converter/", True),
                ("directory", preview + f"/{ROOT.name}/app/static/index.html", False),
            ]:
                page.goto(url, wait_until="networkidle")
                expect(page.locator(".workspace")).to_have_css("display", "grid")
                expect(page.locator(".converter-card")).to_have_css(
                    "background-color", "rgb(255, 255, 255)"
                )
                assert page.evaluate(
                    "[...document.styleSheets].some(sheet => sheet.href?.includes('styles.css') && sheet.cssRules.length > 0)"
                )
                if has_api:
                    expect(page.locator("#dropzone")).to_be_enabled()
                else:
                    expect(page.locator("#connection-error")).to_be_visible()
                for width in [1280, 820, 390, 320]:
                    page.set_viewport_size({"width": width, "height": 960})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                        name,
                        width,
                    )
                page.set_viewport_size({"width": 1280, "height": 960})
                page.screenshot(path=str(ARTIFACTS / f"asset-fix-{name}.png"), full_page=True)
                if name == "proxy":
                    # Confirm both capability lookup and conversion keep the proxy prefix.
                    page.locator("#file-input").set_input_files(
                        {
                            "name": "prefix.txt",
                            "mimeType": "text/plain",
                            "buffer": b"Proxy paths work.",
                        }
                    )
                    page.locator("#convert-button").click()
                    expect(page.locator(".download-button")).to_have_count(1, timeout=60000)
                    with page.expect_download() as download_info:
                        page.get_by_role("link", name="Download prefix.pdf", exact=True).click()
                    download_info.value.save_as(ARTIFACTS / "prefix.pdf")
                    assert (
                        "Proxy paths work."
                        in PdfReader(ARTIFACTS / "prefix.pdf").pages[0].extract_text()
                    )

            page.route("**/styles.css", lambda route: route.abort())
            page.goto(BASE_URL + "/", wait_until="networkidle")
            expect(page.locator("#dropzone")).to_be_enabled()
            assert (
                page.locator(".icon-library").evaluate("node => node.getBoundingClientRect().width")
                == 0
            )
            page.locator("#file-input").set_input_files(
                {"name": "bounded.csv", "mimeType": "text/csv", "buffer": b"name\nAlice\n"}
            )
            expect(page.locator(".file-row")).to_have_count(1)
            for icon in page.locator("svg.icon").all():
                assert icon.evaluate(
                    "node => node.getBoundingClientRect().width <= 24 && node.getBoundingClientRect().height <= 24"
                )
            assert not errors, errors
            browser.close()
        print(
            "PASS: styles load at root, /static/, directory and proxy previews; prefixed PDF conversion works; icons remain bounded without CSS."
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    run()
