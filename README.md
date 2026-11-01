# Anything to Anything

A document conversion web app built with **Python / FastAPI** and a small, dependency-free **JavaScript** frontend. Simple conversions run entirely in the browser; complex ones use Pandoc, LibreOffice, and Python libraries on the server. No accounts, API keys, external conversion service, or database required.

## Run locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Node 20+ is only needed for JavaScript unit tests. Pandoc is included in the locked Python dependencies.

```sh
bash scripts/setup.sh
bash scripts/start.sh
```

The server uses port **8000**. Open the app served by that process; opening the HTML in a file viewer will not provide the conversion API. Set `PORT` and `HOST` to change the address. For automatic reload during development:

```sh
.venv/bin/uvicorn app.main:app --reload --port 8000
```

`CONVERSION_WORKERS` sets the number of simultaneous server conversions (1–4, default 2). `HTTP_CONCURRENCY` limits active HTTP connections (default 20). A small hosted instance should use one conversion worker and a lower HTTP limit.

Install LibreOffice Writer, Calc, and Impress for office PDF export and legacy formats. The app detects either `libreoffice` or `soffice` on `PATH`. On Debian/Ubuntu:

```sh
apt-get update
apt-get install -y libreoffice-writer libreoffice-calc libreoffice-impress fonts-dejavu-core tesseract-ocr tesseract-ocr-eng
```

Without LibreOffice, modern formats still work. Document and presentation PDF export uses a **text-only** fallback; DOC, XLS, PPT, and ODP input and ODP output are not offered. The interface always uses the backend's actual capabilities.

## Run with Docker

```sh
docker compose up --build
```

The image includes LibreOffice and runs as a non-root user. Compose adds a read-only filesystem, a temporary workspace, and memory/CPU limits. Docker support is provided; build it on a machine with a Docker daemon.

If your build network uses a TLS-inspecting proxy, supply your already-trusted public CA bundle through BuildKit rather than disabling certificate verification:

```sh
docker build --secret id=build_ca,src=/path/to/trusted-ca-bundle.pem \
  --build-arg HTTP_PROXY --build-arg HTTPS_PROXY -t anythingtoanything .
```

Proxy arguments use the corresponding exported environment variables. Container DNS must be able to resolve the proxy. The optional CA file is mounted only during dependency installation and is not copied into the image.

## Deploy a public website

The repository includes a Render Blueprint at `render.yaml`. After the code is on the repository's `main` branch, open [Deploy to Render](https://render.com/deploy?repo=https://github.com/zibah04/anythingtoanything) and connect your Render account. Review the Blueprint and create the service. Render builds the Docker image, checks `/api/health`, and assigns the public HTTPS URL; the onboarding environment itself does not expose a public preview.

The initial Blueprint requests a **free** web service, no database, and no persistent disk. It sets port 10000, one conversion worker, and eight HTTP connections. Free services can sleep on idle and have limited memory and compute; first requests may be slow, and large complex files may exceed those resources. The app's file limits are upper bounds, not a memory guarantee for every native parser. Choose a larger always-on plan for regular public traffic only after reviewing its price.

No Render credential is needed by the deployed app. If an agent deploys through the Render API, supply `RENDER_API_KEY` securely in the cloud environment settings with destination `api.render.com`; never commit it or send it in chat. The cloud environment also needs access to `render.com`, `dashboard.render.com`, and `api.render.com`. GitHub API operations need `api.github.com`; native Git uses the existing platform authentication.

After deployment, verify the public page loads its CSS and scripts, `/api/health` returns `ok`, and a real DOCX → PDF conversion returns a readable PDF. The live site is ready only after these checks pass against the assigned public URL.

## Conversion coverage

| Source | Compatible outputs | Where |
| --- | --- | --- |
| DOCX, ODT, RTF, EPUB, Markdown, HTML, TXT | PDF, DOCX, ODT, RTF, EPUB, Markdown, HTML, TXT (except the source format) | Server; TXT → HTML/MD and HTML → TXT run locally |
| PDF | DOCX | Server, **positioned editable text**, **flowing layout reconstruction**, or **page images** |
| PDF | ODT, RTF, EPUB, Markdown, HTML, TXT | Server, **text extraction + optional offline English OCR** for scanned pages |
| XLSX, ODS, CSV, TSV, JSON | XLSX, ODS, CSV, TSV, JSON, HTML, PDF (except the source format) | CSV/TSV/JSON interconversion runs locally; others use the server |
| PPTX | PDF, DOCX, TXT, Markdown, HTML; ODP with LibreOffice | Server |
| PNG, JPEG, WebP, GIF, BMP, TIFF | PNG, JPEG, WebP, GIF, BMP, TIFF, PDF (except the source format); TXT when OCR is installed | PNG/JPEG/WebP interconversion runs locally; OCR runs server-side |
| DOC, XLS, PPT, ODP | Outputs in the corresponding document, spreadsheet, or presentation family | Server, **requires LibreOffice** |

Aliases `.jpeg`, `.tif`, `.htm`, and `.markdown` are accepted.

### What to expect

- **25 MB per file, 10 files per browser batch.** Up to two browser conversions run independently of the server queue. Server requests follow the advertised worker limit (two by default, one on the initial Render configuration), so a long server conversion does not hold up local conversions. Files download individually, and failures don't discard other results.
- **PDF → Word defaults to Precise layout:** keeps the original page sizes and positions, with editable text in floating text boxes over page artwork. It embeds permitted TrueType font subsets and preserves character spacing, including justified text. This works well for printed web pages, forms, labels, and mixed columns. Editing is less flexible than a flowing document: text boxes do not reflow between pages, and table contents are positioned text rather than Word tables. Type 3 fonts, unsupported/restricted fonts, complex scripts, rotated text, and scanned pages remain images; this mode does not claim that every visible word is editable. Font subsets contain the original PDF's characters, so adding characters may require choosing another font in Word. Some viewers may handle floating boxes or embedded fonts differently.
- Choose **Editable layout** for flowing editable paragraphs and native Word tables. It reconstructs fonts, images, columns, and page geometry. Reconstruction is best-effort; forms, unusual layouts, and unavailable fonts can shift. Original line breaks are retained, and equivalent column sections are normalized to avoid extra pages in Word readers.
- Choose **Exact appearance** when matching the original pages matters more than editing text. It places a rendered image of each PDF page into a matching Word page. The text is not editable in this mode, including for scanned PDFs. Scanned text needs OCR to become editable; OCR is not implemented. Encrypted PDFs must be unlocked first.
- PDF-to-Word runs in an isolated subprocess with a 90-second timeout and a 50-page limit. Precise and appearance modes limit rendered images to 48 MB and Word-compatible page sizes. Precise mode also limits font data to 20 MB and editable text boxes to 20,000. Fonts are packaged inside the DOCX, never installed on the server. Embedding permissions are respected; fonts that prohibit editable subset embedding stay in the page artwork. Other PDF text exports retain their 300-page limit.
- LibreOffice preserves office layout when exporting PDF; exact rendering still depends on installed fonts. Without it, PDF output is a readable text document. PPTX → text/document formats extracts slide text, without design or images.
- Spreadsheet exports contain cell values, not formulas, charts, or formatting. XLSX ↔ ODS keeps worksheet names and values. CSV/TSV/JSON/HTML/PDF use the first worksheet. JSON input must be an array of objects or rows; object keys form the column union. JSON output requires unique, non-empty headers. Stored formula results may be empty if the source workbook has never been recalculated.
- Text files must use UTF-8 or BOM-marked UTF-16. CSV/TSV exports prefix formula-like string cells with an apostrophe to avoid spreadsheet formula execution. Imported XLSX strings remain strings.
- Image → TXT and scanned PDF text extraction use **offline English Tesseract OCR**, if installed. OCR handles up to 8 image-only PDF pages per document, takes a maximum of 12 seconds per scanned page, and may miss handwriting, low-contrast, tiny or rotated text. An empty scan returns an actionable error rather than an empty output.
- HTML → PDF produces a text-only PDF even when LibreOffice is installed; this deliberately avoids LibreOffice fetching remote HTML resources from untrusted uploads. HTML images, page styling, and layout are not retained in that route.
- Image conversion uses the first frame, applies EXIF orientation, and replaces transparency with white for JPEG/PDF. Animated GIFs and multipage TIFFs are not preserved as animations or multipage documents.
- Pandoc runs with its sandbox enabled. Remote images and linked files are not fetched. Some fonts, complex layouts, equations, or embedded media may not survive a format change.
- Processing limits: 25-megapixel images, 50-page PDF-to-Word / 300-page PDF text extraction, 1 million text characters, 200,000 spreadsheet cells, 128 MB expanded archives, 64 MB output. PDF tables are limited to 2,000 rows and 15 columns. These limits fail with a visible error.

## Privacy and deployment

Local conversions do not upload their files. Server conversions use a unique temporary directory per operation and delete it on success or failure. Results are returned directly with `Cache-Control: no-store`; there are no retained files or public download URLs. The browser holds originals and results in memory until they are removed or the tab closes. No cookies or analytics are used. Browser downloads are saved only when requested.

This is a working first version, designed to run locally or on a trusted server. For a public multi-user deployment, terminate HTTPS at a reverse proxy, enforce request-body/rate limits there, and isolate conversion workers from outbound networking and private infrastructure. The app has no authentication, durable job queue, OCR, or malware scanner. A process crash can leave OS temporary files until the container/host cleans them; Compose's temporary filesystem makes these ephemeral.

The API enforces upload sizes, validates document container signatures, caps archive expansion, limits conversion concurrency, and times out Pandoc/LibreOffice subprocesses. Native parsers still process untrusted files, so deploy with the supplied process/container limits rather than exposing an unrestricted worker. Do not mount sensitive host directories into a public conversion container.

## API

- `GET /api/health` — liveness and version.
- `GET /api/formats` — supported sources, compatible targets, browser/server routing, limitations, and limits.
- `POST /api/convert` — multipart `file` and `target` (extension, such as `pdf`). PDF → DOCX also accepts `pdf_mode=precise` (default), `pdf_mode=editable`, or `pdf_mode=appearance`. Returns the converted file as an attachment. Every advertised browser conversion also works through this endpoint.
- `GET /openapi.json` — the full OpenAPI specification. No externally hosted documentation assets are needed.

Example:

```sh
curl --fail-with-body -F file=@report.docx -F target=pdf \
  http://127.0.0.1:8000/api/convert --output report.pdf
```

PDF-to-Word example:

```sh
curl --fail-with-body -F file=@report.pdf -F target=docx -F pdf_mode=precise \
  http://127.0.0.1:8000/api/convert --output report.docx
```

## Tests

```sh
.venv/bin/pytest -q
npm test
.venv/bin/ruff check app tests
```

The Python suite converts real sample files through every advertised source/target pair and opens the outputs with independent readers. It also tests the API, bad inputs, formula handling, limits, and cleanup. Legacy-format checks run when LibreOffice is available; fallback conversions are checked separately.

`tests/test_pdf_word.py` checks editable tables, embedded images, heading styles, two-column reading order, and mixed portrait/landscape pages. It independently renders the resulting Word files with LibreOffice to check pagination and the visible content of appearance mode. It also covers scanned/encrypted PDFs, page limits, API mode selection, and cleanup.

`tests/test_pdf_precise.py` adds generated, anonymous form regressions for embedded font subsets, font embedding permissions, fractional font sizes, character/word spacing, fixed field positions, page artwork, and mixed page sizes. It renders the DOCX independently with LibreOffice and checks text coordinates, page counts, and visible content. User-uploaded documents are not committed as fixtures.

For real browser tests, start the server, then run:

```sh
UV_CACHE_DIR="$PWD/.cache/uv" uv sync --frozen --group browser
# If system Chromium is unavailable:
.venv/bin/playwright install chromium
.venv/bin/python tests/browser_smoke.py
```

The browser suite verifies a mixed local/server batch, that local files aren't uploaded, downloaded file contents, error messages, dialogs, and mobile layouts. Screenshots and downloads go into ignored `artifacts/`. Set `TEST_BASE_URL` or `BROWSER_EXECUTABLE` if needed.

`python tests/browser_asset_paths.py` checks stylesheet loading from the normal app, the `/static/` entry point, a directory preview, and a reverse proxy mounted under a URL prefix. It also checks that icons stay bounded if the stylesheet fails to load. Run it with the browser dependency group installed and the server running.

`.venv/bin/python tests/browser_pdf_word.py` checks both PDF-to-Word modes in the real browser, verifies the downloaded DOCX contents, checks that changing modes invalidates an old result, and exercises the controls at 390px and 320px widths.

## Project structure

```text
app/main.py             HTTP API, guards, and static serving
app/formats.py          Compatibility and processing-route registry
app/conversion.py       Server conversion engines and limits
app/pdf_word.py         Isolated PDF-to-Word layout and page-image worker
app/static/             Responsive interface and local converters
tests/                  Conversion matrix, API, JS, and browser checks
scripts/                Repeatable setup and startup
```

## PDF-to-Word dependencies

The PDF-to-Word worker uses [pdf2docx](https://github.com/ArtifexSoftware/pdf2docx) (MIT) and [PyMuPDF](https://pymupdf.readthedocs.io/) (AGPL-3.0 or an Artifex commercial license). Their upstream license terms apply to use and distribution of those components.
