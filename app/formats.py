"""A single compatibility registry shared by the API and web interface."""

import shutil

LABELS = {
    "pdf": "PDF",
    "docx": "Word",
    "doc": "Word 97–2003",
    "odt": "OpenDocument",
    "rtf": "Rich text",
    "epub": "EPUB",
    "md": "Markdown",
    "txt": "Plain text",
    "html": "HTML",
    "csv": "CSV",
    "tsv": "TSV",
    "json": "JSON",
    "xlsx": "Excel",
    "xls": "Excel 97–2003",
    "ods": "OpenDocument sheet",
    "pptx": "PowerPoint",
    "ppt": "PowerPoint 97–2003",
    "odp": "OpenDocument slides",
    "png": "PNG",
    "jpg": "JPEG",
    "webp": "WebP",
    "gif": "GIF",
    "bmp": "Bitmap",
    "tiff": "TIFF",
}
ALIASES = {"jpeg": "jpg", "tif": "tiff", "htm": "html", "markdown": "md"}
DOCUMENTS = ("docx", "pdf", "txt", "md", "html", "odt", "rtf", "epub")
TABLES = ("xlsx", "csv", "tsv", "json", "ods")
IMAGES = ("png", "jpg", "webp", "gif", "bmp", "tiff")
SLIDES = ("pptx",)
PDF_WORD_MODES = [
    {
        "value": "precise",
        "label": "Precise layout",
        "note": "Preserves page positions with editable text boxes. Unsupported text, scans, and artwork stay as images. Best for forms and fixed layouts.",
    },
    {
        "value": "editable",
        "label": "Editable layout",
        "note": "Reconstructs editable text, tables, and images. Complex layouts and fonts can still shift.",
    },
    {
        "value": "appearance",
        "label": "Exact appearance",
        "note": "Keeps each page as an image inside Word. Original appearance is retained, but text is not editable. Also works for scanned pages.",
    },
]


def normalize(extension: str) -> str:
    value = extension.lower().lstrip(".")
    return ALIASES.get(value, value)


def office_available() -> bool:
    return bool(shutil.which("libreoffice") or shutil.which("soffice"))


def target_map() -> dict[str, list[str]]:
    result = {ext: [x for x in DOCUMENTS if x != ext] for ext in DOCUMENTS}
    result.update({ext: [x for x in (*TABLES, "html", "pdf") if x != ext] for ext in TABLES})
    result.update({ext: [x for x in (*IMAGES, "pdf") if x != ext] for ext in IMAGES})
    result["pptx"] = ["pdf", "txt", "md", "html", "docx"]
    if office_available():
        result["doc"] = list(DOCUMENTS)
        result["xls"] = [*TABLES, "html", "pdf"]
        result["ppt"] = ["pptx", "pdf", "txt", "md", "html", "docx"]
        result["odp"] = ["pptx", "pdf", "txt", "md", "html", "docx"]
        result["pptx"].append("odp")
    return result


def browser_route(source: str, target: str) -> bool:
    return (
        source in {"csv", "tsv", "json"}
        and target in {"csv", "tsv", "json"}
        or source in {"png", "jpg", "webp"}
        and target in {"png", "jpg", "webp"}
        or (source, target) in {("txt", "html"), ("html", "txt"), ("txt", "md")}
    )


def conversion_note(source: str, target: str) -> str | None:
    if source == "pdf":
        if target == "docx":
            return PDF_WORD_MODES[0]["note"]
        return "Extracts text. Original layout, images, and scanned pages are not preserved."
    if source in {"pptx", "ppt", "odp"}:
        if target in {"pdf", "odp", "pptx"} and office_available():
            return None
        return "Extracts slide text; slide design, charts, and images are not preserved."
    if source in TABLES or source == "xls":
        if target in {"csv", "tsv", "json", "html", "pdf"}:
            return "Uses the first worksheet and stored cell values; formulas and styling are not preserved."
        return "Converts worksheet values. Formulas, charts, and styling are not preserved."
    if source in IMAGES:
        return "Uses the first frame. JPEG replaces transparency with white."
    if target == "pdf" and not office_available():
        return "Creates a readable text PDF. Images and original page layout are not preserved."
    if target in {"txt", "md"}:
        return "Some formatting and embedded media may be lost."
    if source in {"html", "md"}:
        return "Remote images and linked files are not fetched. Some styling may change."
    return None


def registry() -> dict:
    formats = []
    for extension, targets in target_map().items():
        family = (
            "Images"
            if extension in IMAGES
            else "Spreadsheets"
            if extension in TABLES or extension == "xls"
            else "Presentations"
            if extension in {"pptx", "ppt", "odp"}
            else "Documents"
        )
        formats.append(
            {
                "extension": extension,
                "label": LABELS[extension],
                "family": family,
                "targets": [
                    {
                        "extension": target,
                        "label": LABELS[target],
                        "engine": "browser" if browser_route(extension, target) else "server",
                        "note": conversion_note(extension, target),
                    }
                    for target in targets
                ],
            }
        )
    return {
        "formats": formats,
        "max_file_size": 25 * 1024 * 1024,
        "max_batch_size": 10,
        "office_available": office_available(),
        "pdf_word_modes": PDF_WORD_MODES,
    }
