"""Bounded, temporary-file-based conversion. No document content is retained."""

import csv
import html
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import warnings
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree

import pypandoc
from openpyxl import Workbook, load_workbook
from PIL import Image, ImageOps, UnidentifiedImageError
from pptx import Presentation
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.formats import DOCUMENTS, IMAGES, TABLES, normalize, office_available, target_map

MAX_TEXT = 1_000_000
PDF_WORD_TIMEOUT = 90
MAX_CELLS = 200_000
MAX_OUTPUT = 64 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000


class ConversionError(Exception):
    """An actionable, safe-to-display conversion failure."""


def validate_archive(source: Path) -> None:
    if not zipfile.is_zipfile(source):
        return
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > 4096 or sum(m.file_size for m in members) > 128 * 1024 * 1024:
            raise ConversionError("This document expands beyond the safe processing limit.")
        for member in members:
            if member.flag_bits & 1:
                raise ConversionError("Password-protected documents are not supported.")


def decode_text(source: Path) -> str:
    data = source.read_bytes()
    try:
        text = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeError as exc:
        raise ConversionError("Use a UTF-8 or UTF-16 encoded text file.") from exc
    if "\x00" in text:
        raise ConversionError("This file contains binary data, not readable text.")
    if len(text) > MAX_TEXT:
        raise ConversionError("Text content exceeds the 1 million character processing limit.")
    return text


PANDOC_FORMATS = {
    "md": "gfm",
    "txt": "plain",
    "html": "html",
    "docx": "docx",
    "odt": "odt",
    "rtf": "rtf",
    "epub": "epub",
}


def pandoc_convert(source: Path, source_ext: str, target: Path, target_ext: str) -> None:
    reader = PANDOC_FORMATS[source_ext]
    if source_ext == "txt":
        # Wrap literal text instead of interpreting its Markdown punctuation.
        literal = source.with_name("literal.html")
        literal.write_text(
            f"<html><body><pre>{html.escape(decode_text(source))}</pre></body></html>",
            encoding="utf-8",
        )
        source, reader = literal, "html"
    command = [
        pypandoc.get_pandoc_path(),
        str(source),
        "--from",
        reader,
        "--to",
        PANDOC_FORMATS[target_ext],
        "--output",
        str(target),
        "--sandbox",
        "--standalone",
        "--wrap=none",
    ]
    if target_ext == "html":
        command.append("--embed-resources")
    try:
        result = subprocess.run(
            command,
            check=False,
            cwd=source.parent,
            capture_output=True,
            timeout=45,
            env={**os.environ, "LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(
            "This document took too long to convert. Try a smaller file."
        ) from exc
    if result.returncode or not target.is_file():
        raise ConversionError(
            "The document could not be read. Check that its contents match its extension."
        )


def office_convert(source: Path, target: Path, target_ext: str) -> None:
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        raise ConversionError("This conversion requires LibreOffice on the server.")
    profile = source.parent / "office-profile"
    user_profile = profile / "user"
    user_profile.mkdir(parents=True, exist_ok=True)
    (user_profile / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
        '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
        "</item></oor:items>",
        encoding="utf-8",
    )
    outdir = source.parent / "office-output"
    outdir.mkdir(exist_ok=True)
    try:
        result = subprocess.run(
            [
                executable,
                f"-env:UserInstallation={profile.as_uri()}",
                "--headless",
                "--nologo",
                "--nodefault",
                "--norestore",
                "--convert-to",
                target_ext,
                "--outdir",
                str(outdir),
                str(source),
            ],
            check=False,
            capture_output=True,
            timeout=60,
            cwd=source.parent,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("The office conversion timed out. Try a smaller document.") from exc
    converted = outdir / f"{source.stem}.{target_ext}"
    if result.returncode or not converted.is_file():
        raise ConversionError(
            "LibreOffice could not convert this document. Check its format and password protection."
        )
    shutil.move(converted, target)


def extract_text(source: Path, extension: str) -> str:
    if extension == "pdf":
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise ConversionError("Unlock this PDF before converting it.")
        if len(reader.pages) > 300:
            raise ConversionError("PDF text extraction is limited to 300 pages.")
        parts, size = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            size += len(text)
            if size > MAX_TEXT:
                raise ConversionError("Extracted text exceeds the processing limit.")
            parts.append(text)
        text = "\n\n".join(parts)
        if not text.strip():
            raise ConversionError(
                "No selectable text was found. Scanned PDFs need OCR, which is not supported yet."
            )
        return text
    if extension == "pptx":
        presentation = Presentation(source)
        parts = []
        for index, slide in enumerate(presentation.slides, 1):
            texts = [shape.text for shape in slide.shapes if shape.has_text_frame]
            parts.append(f"Slide {index}\n" + "\n".join(texts))
        text = "\n\n".join(parts)
    elif extension == "txt":
        return decode_text(source)
    else:
        output = source.with_name("extracted.txt")
        pandoc_convert(source, extension, output, "txt")
        return decode_text(output)
    if not text.strip():
        raise ConversionError("No readable text was found in this document.")
    if len(text) > MAX_TEXT:
        raise ConversionError("Extracted text exceeds the processing limit.")
    return text


def pdf_styles():
    styles = getSampleStyleSheet()
    font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if font.is_file():
        if "DocumentSans" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("DocumentSans", str(font)))
        styles["BodyText"].fontName = "DocumentSans"
    styles["BodyText"].fontSize = 10
    styles["BodyText"].leading = 16
    styles["BodyText"].wordWrap = "CJK"
    return styles


def text_pdf(text: str, target: Path) -> None:
    styles = pdf_styles()
    story = []
    for line in text.splitlines():
        # Bound each Paragraph's size to keep very long lines tractable.
        for start in range(0, max(len(line), 1), 2000):
            fragment = line[start : start + 2000]
            story.append(
                Paragraph(html.escape(fragment) if fragment else "&#160;", styles["BodyText"])
            )
        if not line:
            story.append(Spacer(1, 4))
    SimpleDocTemplate(str(target), title="Converted document").build(story)


def read_table(source: Path, extension: str) -> list[tuple[str, list[list]]]:
    if extension == "xlsx":
        workbook = load_workbook(source, read_only=True, data_only=True, keep_links=False)
        sheets = []
        cells = 0
        try:
            for sheet in workbook.worksheets:
                if (
                    sheet.max_row
                    and sheet.max_column
                    and sheet.max_row * sheet.max_column > MAX_CELLS
                ):
                    raise ConversionError("This spreadsheet exceeds the 200,000 cell limit.")
                rows = []
                for row in sheet.iter_rows(values_only=True):
                    cells += len(row)
                    if cells > MAX_CELLS:
                        raise ConversionError("This spreadsheet exceeds the 200,000 cell limit.")
                    rows.append(list(row))
                sheets.append((sheet.title, rows))
        finally:
            workbook.close()
        return sheets
    if extension == "ods":
        # ODS values are readable without loading scripts or external resources.
        with zipfile.ZipFile(source) as archive:
            content = archive.read("content.xml")
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise ConversionError("XML entity declarations are not allowed.")
        root = ElementTree.fromstring(content)
        ns = {
            "t": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
            "x": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
            "o": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
        }
        sheets, cells = [], 0
        for table in root.findall(".//t:table", ns):
            rows = []
            for row in table.findall("t:table-row", ns):
                values = []
                for cell in row.findall("t:table-cell", ns):
                    repeat = int(cell.get(f"{{{ns['t']}}}number-columns-repeated", "1"))
                    text = "\n".join("".join(p.itertext()) for p in cell.findall("x:p", ns))
                    value_type = cell.get(f"{{{ns['o']}}}value-type", "string")
                    value = text
                    if value_type in {"float", "percentage", "currency"}:
                        value = float(cell.get(f"{{{ns['o']}}}value", "0"))
                        if value.is_integer():
                            value = int(value)
                    elif value_type == "boolean":
                        value = cell.get(f"{{{ns['o']}}}boolean-value") == "true"
                    elif value_type == "date":
                        value = datetime.fromisoformat(
                            cell.get(f"{{{ns['o']}}}date-value")
                        ).replace(tzinfo=None)
                    if repeat > MAX_CELLS or len(values) + repeat > MAX_CELLS:
                        raise ConversionError("This spreadsheet exceeds the cell processing limit.")
                    values.extend([value] * repeat)
                count = int(row.get(f"{{{ns['t']}}}number-rows-repeated", "1"))
                if count > MAX_CELLS or cells + max(1, len(values)) * count > MAX_CELLS:
                    raise ConversionError("This spreadsheet exceeds the cell processing limit.")
                cells += max(1, len(values)) * count
                rows.extend([values.copy() for _ in range(count)])
            sheets.append((table.get(f"{{{ns['t']}}}name", "Sheet"), rows))
        return sheets or [("Sheet1", [])]
    if extension in {"csv", "tsv"}:
        rows = list(
            csv.reader(
                io.StringIO(decode_text(source), newline=""),
                delimiter="\t" if extension == "tsv" else ",",
                strict=True,
            )
        )
    else:
        data = json.loads(decode_text(source))
        if not isinstance(data, list):
            raise ConversionError(
                "For spreadsheet conversion, JSON must be an array of objects or rows."
            )
        if not data:
            return [("Sheet1", [])]
        if all(isinstance(row, dict) for row in data):
            headers = list(dict.fromkeys(key for row in data for key in row))
            rows = [headers] + [[row.get(key, "") for key in headers] for row in data]
        elif all(isinstance(row, list) for row in data):
            rows = data
        else:
            raise ConversionError("JSON must contain only objects, or only arrays of cell values.")
        rows = [
            [
                json.dumps(cell, ensure_ascii=False) if isinstance(cell, (dict, list)) else cell
                for cell in row
            ]
            for row in rows
        ]
    if sum(len(row) for row in rows) > MAX_CELLS:
        raise ConversionError("This spreadsheet exceeds the 200,000 cell limit.")
    return [("Sheet1", rows)]


def table_html(rows: list[list]) -> str:
    body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(str(cell) if cell is not None else '')}</td>" for cell in row)
        + "</tr>"
        for row in rows
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Converted table</title>"
        "<style>body{font:15px system-ui;margin:40px}table{border-collapse:collapse}"
        "td{border:1px solid #ccc;padding:8px 12px}</style></head><body><table>"
        + body
        + "</table></body></html>"
    )


def table_convert(source: Path, source_ext: str, target: Path, target_ext: str) -> None:
    sheets = read_table(source, source_ext)
    rows = sheets[0][1]
    if target_ext in {"csv", "tsv"}:
        with target.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t" if target_ext == "tsv" else ",")
            # Prevent spreadsheet formula execution when opening exported text tables.
            writer.writerows(
                [
                    [
                        "'" + cell
                        if isinstance(cell, str)
                        and cell.startswith(("=", "+", "-", "@", "\t", "\r"))
                        else cell
                        for cell in row
                    ]
                    for row in rows
                ]
            )
    elif target_ext == "json":
        if not rows:
            data = []
        else:
            headers = [str(cell) if cell is not None else "" for cell in rows[0]]
            if len(set(headers)) != len(headers) or any(not header for header in headers):
                raise ConversionError(
                    "JSON export needs unique, non-empty column headers in the first row."
                )
            if any(len(row) > len(headers) for row in rows[1:]):
                raise ConversionError("A row has more cells than there are column headers.")
            data = [dict(zip(headers, row + [""] * (len(headers) - len(row)))) for row in rows[1:]]
        target.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
    elif target_ext == "ods":
        write_ods(sheets, target)
    elif target_ext == "xlsx":
        workbook = Workbook()
        workbook.remove(workbook.active)
        for name, sheet_rows in sheets:
            sheet = workbook.create_sheet(name)
            for row_number, row in enumerate(sheet_rows, 1):
                for column, value in enumerate(row, 1):
                    cell = sheet.cell(row_number, column, value)
                    if cell.data_type == "f":
                        cell.data_type = "s"  # Preserve imported text without activating formulas.
        workbook.save(target)
    elif target_ext == "html":
        target.write_text(table_html(rows), encoding="utf-8")
    elif target_ext == "pdf":
        if not rows:
            raise ConversionError("This table is empty.")
        if len(rows) > 2000 or max(map(len, rows), default=0) > 15:
            raise ConversionError(
                "PDF table export supports up to 2,000 rows and 15 columns. Choose XLSX or CSV for larger sheets."
            )
        width = max(map(len, rows))
        style = pdf_styles()["BodyText"]
        style.fontSize, style.leading = 7, 10
        data = [
            [
                Paragraph(html.escape(str(cell) if cell is not None else ""), style)
                for cell in row + [""] * (width - len(row))
            ]
            for row in rows
        ]
        table = Table(data, colWidths=[720 / width] * width, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2ee")),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        SimpleDocTemplate(str(target), pagesize=(842, 595), leftMargin=40, rightMargin=40).build(
            [table]
        )


def write_ods(sheets: list, target: Path) -> None:
    ns = "urn:oasis:names:tc:opendocument:xmlns:"
    content = [
        (
            f'<office:document-content xmlns:office="{ns}office:1.0" '
            f'xmlns:table="{ns}table:1.0" xmlns:text="{ns}text:1.0" office:version="1.2">'
            "<office:body><office:spreadsheet>"
        )
    ]
    for name, rows in sheets:
        content.append(f'<table:table table:name="{html.escape(name, quote=True)}">')
        for row in rows:
            content.append("<table:table-row>")
            for value in row:
                text = "" if value is None else str(value)
                attributes = 'office:value-type="string"'
                if isinstance(value, bool):
                    attributes = (
                        f'office:value-type="boolean" office:boolean-value="{str(value).lower()}"'
                    )
                elif isinstance(value, (int, float)):
                    attributes = f'office:value-type="float" office:value="{value}"'
                elif isinstance(value, (date, datetime)):
                    attributes = f'office:value-type="date" office:date-value="{value.isoformat()}"'
                content.append(
                    f"<table:table-cell {attributes}><text:p>"
                    + html.escape(text)
                    + "</text:p></table:table-cell>"
                )
            content.append("</table:table-row>")
        content.append("</table:table>")
    content.append("</office:spreadsheet></office:body></office:document-content>")
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.spreadsheet",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("content.xml", '<?xml version="1.0" encoding="UTF-8"?>' + "".join(content))
        archive.writestr(
            "META-INF/manifest.xml",
            '<?xml version="1.0"?>'
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
            "</manifest:manifest>",
        )


def image_convert(source: Path, target: Path, target_ext: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original)
                image.load()
                if target_ext in {"jpg", "pdf"}:
                    rgba = image.convert("RGBA")
                    flattened = Image.new("RGB", image.size, "white")
                    flattened.paste(rgba, mask=rgba.getchannel("A"))
                    image = flattened
                output_format = {"jpg": "JPEG", "tiff": "TIFF"}.get(target_ext, target_ext.upper())
                image.save(target, format=output_format, quality=92)
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise ConversionError("This image exceeds the 25 megapixel processing limit.") from exc


def pdf_word_convert(source: Path, target: Path, mode: str) -> None:
    if mode not in {"precise", "editable", "ocr", "appearance"}:
        raise ConversionError(
            "Choose Precise layout, Editable layout, Scanned text (OCR), or Exact appearance for PDF-to-Word conversion."
        )
    status = source.with_name("pdf-word-status.json")
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.pdf_word",
                str(source),
                str(target),
                "--mode",
                mode,
                "--status",
                str(status),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
            cwd=Path(__file__).resolve().parents[1],
            env={
                **os.environ,
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
            },
        ) as worker:
            try:
                worker.communicate(timeout=PDF_WORD_TIMEOUT)
            except subprocess.TimeoutExpired:
                # Stop the entire isolated group, including an active OCR child.
                if os.name == "posix":
                    try:
                        os.killpg(worker.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    worker.kill()
                worker.communicate()
                raise
            returncode = worker.returncode
    except subprocess.TimeoutExpired as error:
        raise ConversionError(
            "PDF-to-Word timed out. Split the PDF into fewer pages and try again."
        ) from error
    outcome = json.loads(status.read_text(encoding="utf-8")) if status.is_file() else {}
    if returncode or not outcome.get("ok"):
        raise ConversionError(
            outcome.get("message", "The PDF conversion could not finish. Try a smaller PDF.")
        )


def convert_bytes(
    data: bytes, source_ext: str, target_ext: str, pdf_mode: str = "precise"
) -> bytes:
    source_ext, target_ext = normalize(source_ext), normalize(target_ext)
    if target_ext not in target_map().get(source_ext, []):
        raise ConversionError(
            "This pair of formats is not supported. Choose one of the listed output formats."
        )
    with tempfile.TemporaryDirectory(prefix="ata-") as directory:
        root = Path(directory)
        source, target = root / f"input.{source_ext}", root / f"converted.{target_ext}"
        source.write_bytes(data)
        if source_ext in {
            "docx",
            "odt",
            "epub",
            "xlsx",
            "ods",
            "pptx",
            "odp",
        } and not zipfile.is_zipfile(source):
            raise ConversionError(
                "This file could not be read as its stated document format. Check its extension."
            )
        if source_ext in {"doc", "xls", "ppt"} and not data.startswith(
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        ):
            raise ConversionError(
                "This file could not be read as a legacy Office document. Check its extension."
            )
        if source_ext in {"txt", "md", "html"}:
            decode_text(source)
        validate_archive(source)
        try:
            if source_ext in {"doc", "xls", "ppt", "odp"}:
                normalized = {"doc": "docx", "xls": "xlsx", "ppt": "pptx", "odp": "pptx"}[
                    source_ext
                ]
                intermediate = root / f"normalized.{normalized}"
                office_convert(source, intermediate, normalized)
                source, source_ext = intermediate, normalized
                if normalized == target_ext:
                    return source.read_bytes()
            if source_ext in IMAGES:
                image_convert(source, target, target_ext)
            elif source_ext in TABLES:
                table_convert(source, source_ext, target, target_ext)
            elif source_ext == "pptx" and target_ext in {"pdf", "odp"} and office_available():
                office_convert(source, target, target_ext)
            elif target_ext == "pdf":
                if office_available() and source_ext in {"docx", "odt", "rtf", "html", "txt"}:
                    office_convert(source, target, "pdf")
                else:
                    text_pdf(extract_text(source, source_ext), target)
            elif source_ext == "pdf" and target_ext == "docx":
                pdf_word_convert(source, target, pdf_mode)
            elif source_ext in {"pdf", "pptx"}:
                text = extract_text(source, source_ext)
                plain = root / "extracted.txt"
                plain.write_text(text, encoding="utf-8")
                if target_ext == "txt":
                    shutil.copyfile(plain, target)
                else:
                    pandoc_convert(plain, "txt", target, target_ext)
            elif source_ext in DOCUMENTS:
                if source_ext in {"txt", "md", "html"}:
                    decode_text(source)
                pandoc_convert(source, source_ext, target, target_ext)
            if not target.is_file() or target.stat().st_size == 0:
                raise ConversionError("The conversion produced an empty file.")
            if target.stat().st_size > MAX_OUTPUT:
                raise ConversionError("The converted file exceeds the 64 MB output limit.")
            return target.read_bytes()
        except ConversionError:
            raise
        except (
            UnidentifiedImageError,
            ValueError,
            KeyError,
            OSError,
            csv.Error,
            ElementTree.ParseError,
            zipfile.BadZipFile,
        ) as exc:
            raise ConversionError(
                "This file could not be read. Check that it is valid, unencrypted, and matches its extension."
            ) from exc
