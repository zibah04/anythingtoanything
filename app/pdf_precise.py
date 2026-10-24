"""Positioned Word text over PDF artwork, without reconstructing page flow.

Only text that can be represented safely is removed from the artwork. Type 3
fonts, rotated text, complex scripts, and restricted fonts stay in that artwork.
Embedded TrueType subsets are packaged in the DOCX, never installed on the host.
"""

import hashlib
import io
import math
import struct
import unicodedata
import uuid
from dataclasses import dataclass

import pymupdf
from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION_START
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Pt
from fontTools.ttLib import TTFont, TTLibError
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from lxml import etree

from app.pdf_word import MAX_IMAGE_BYTES, MAX_PAGE_PIXELS, PDFWordError, place_page_image

MAX_FONTS_BYTES = 20 * 1024 * 1024
MAX_TEXT_BOXES = 20_000
BUILTIN_FONTS = {
    "Helvetica": ("Arial", "helv"),
    "Helvetica-Bold": ("Arial", "hebo"),
    "Helvetica-Oblique": ("Arial", "heit"),
    "Helvetica-BoldOblique": ("Arial", "hebi"),
    "Times-Roman": ("Times New Roman", "tiro"),
    "Times-Bold": ("Times New Roman", "tibo"),
    "Times-Italic": ("Times New Roman", "tiit"),
    "Times-BoldItalic": ("Times New Roman", "tibi"),
    "Courier": ("Courier New", "cour"),
    "Courier-Bold": ("Courier New", "cobo"),
    "Courier-Oblique": ("Courier New", "coit"),
    "Courier-BoldOblique": ("Courier New", "cobi"),
}


@dataclass
class Font:
    family: str
    ascent: float
    descent: float
    widths: dict[int, float]
    bold: bool = False
    italic: bool = False
    builtin: object = None

    def advance(self, character):
        if self.builtin:
            return self.builtin.text_length(character, fontsize=1)
        return self.widths[ord(character)]

    def supports(self, characters):
        return all(
            character.isprintable()
            and unicodedata.bidirectional(character) not in {"R", "AL", "AN"}
            and not unicodedata.combining(character)
            and (self.builtin and ord(character) < 256 or ord(character) in self.widths)
            for character in characters
        )


def element(tag, **attributes):
    node = OxmlElement(tag)
    for name, value in attributes.items():
        node.set(qn("w:" + name), str(value))
    return node


class Fonts:
    def __init__(self, document, pdf):
        self.document = document
        self.pdf = pdf
        self.cache = {}
        self.embedded = {}
        self.character_maps = None
        self.total_bytes = 0

    def for_page(self, page):
        if self.character_maps is None:
            self.character_maps = {}
            # Some PDF subsets only have a Mac/byte-code cmap. Recover their
            # Unicode glyph mapping from the PDF text interpreter's glyph IDs,
            # across all pages before packaging any subset in the Word file.
            for source_page in self.pdf:
                references = {}
                for item in source_page.get_fonts(full=True):
                    references.setdefault(item[3].split("+")[-1], set()).add(item[0])
                for trace in source_page.get_texttrace():
                    candidates = references.get(trace["font"], set())
                    if len(candidates) != 1:
                        continue
                    xref = next(iter(candidates))
                    mapping = self.character_maps.setdefault(xref, {})
                    for code, glyph, *_ in trace["chars"]:
                        if 0 <= code <= 0x10FFFF and glyph >= 0:
                            mapping[code] = glyph
        fonts = {}
        for xref, extension, kind, name, *_ in page.get_fonts(full=True):
            name = name.split("+")[-1]
            if name in BUILTIN_FONTS and extension != "ttf":
                family, alias = BUILTIN_FONTS[name]
                metric = pymupdf.Font(alias)
                fonts[name] = Font(
                    family,
                    metric.ascender,
                    -metric.descender,
                    {},
                    "Bold" in name,
                    "Italic" in name or "Oblique" in name,
                    metric,
                )
            elif extension == "ttf" and kind != "Type3":
                if xref not in self.cache:
                    self.cache[xref] = self.embed(xref, self.character_maps.get(xref, {}))
                if self.cache[xref]:
                    fonts[name] = self.cache[xref]
        return fonts

    def embed(self, xref, characters=None):
        data = self.pdf.extract_font(xref)[3]
        self.total_bytes += len(data)
        if self.total_bytes > MAX_FONTS_BYTES:
            raise PDFWordError("Embedded fonts exceed the 20 MB budget. Split this PDF first.")
        if not data:
            return None
        digest = hashlib.sha256(
            data + repr(sorted((characters or {}).items())).encode()
        ).hexdigest()[:16]
        if digest in self.embedded:
            return self.embedded[digest]
        try:
            font = TTFont(io.BytesIO(data))
            # Preview/print-only fonts cannot be embedded in an editable file.
            # Also respect restricted, bitmap-only, and no-subsetting permissions.
            if font["OS/2"].fsType & 0x0306 or "glyf" not in font:
                return None
            cmap = font.getBestCmap()
            if not cmap and characters:
                cmap = {
                    code: font.getGlyphName(glyph)
                    for code, glyph in characters.items()
                    if glyph < font["maxp"].numGlyphs
                }
                table = CmapSubtable.newSubtable(12 if max(cmap, default=0) > 0xFFFF else 4)
                table.platformID = 3
                table.platEncID = 10 if table.format == 12 else 1
                table.language = 0
                table.cmap = cmap
                font["cmap"].tables.append(table)
            if not cmap:
                return None
            units = font["head"].unitsPerEm
            widths = {code: font["hmtx"][glyph][0] / units for code, glyph in cmap.items()}
            # PDFs can have different subsets of the same family on different pages.
            # Give each subset a distinct family so readers cannot substitute one
            # subset for another and turn missing letters into blank glyphs.
            family = "PDF " + digest
            for record in font["name"].names:
                name = {
                    1: family,
                    2: "Regular",
                    4: family,
                    6: family.replace(" ", ""),
                    16: family,
                    17: "Regular",
                }.get(record.nameID)
                if name:
                    record.string = name.encode(record.getEncoding(), errors="replace")
            font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~0x21) | 0x40
            font["head"].macStyle &= ~3
            stream = io.BytesIO()
            font.save(stream)
            model = Font(family, font["hhea"].ascent / units, -font["hhea"].descent / units, widths)
        except (ValueError, KeyError, AssertionError, TTLibError, struct.error, OSError):
            # A font we cannot embed remains visible in the original artwork.
            return None
        finally:
            if "font" in locals():
                font.close()
        key = uuid.uuid4()
        mask = key.bytes[::-1]
        obfuscated = bytearray(stream.getvalue())
        for index in range(min(32, len(obfuscated))):
            obfuscated[index] ^= mask[index % 16]
        font_table = self.document.part.part_related_by(RT.FONT_TABLE)
        part = Part(
            PackURI(f"/word/fonts/{family.replace(' ', '')}.odttf"),
            "application/vnd.openxmlformats-officedocument.obfuscatedFont",
            bytes(obfuscated),
            self.document.part.package,
        )
        relationship = font_table.relate_to(part, RT.FONT)
        entry = element("w:font", name=family)
        embedding = element("w:embedRegular", fontKey="{" + str(key).upper() + "}")
        embedding.set(qn("r:id"), relationship)
        entry.append(embedding)
        table = etree.fromstring(font_table.blob)
        table.append(entry)
        font_table._blob = etree.tostring(table)
        self.embedded[digest] = model
        return model


def text_box(paragraph, span, font, identifier):
    chars = span["chars"]
    size = max(0.5, round(span["size"] * 2) / 2)
    scale = max(1, min(600, round(span["size"] / size * 100)))
    height = (font.ascent + font.descent) * size
    x, baseline = chars[0]["origin"]
    # Exact-height Word lines reserve 20% below the baseline. Using this line
    # origin avoids clipping small labels or shifting fonts with tall descenders.
    y = baseline - height * 0.8
    width = max(1, span["bbox"][2] - x)
    shape = parse_xml(
        '<v:shape xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office" '
        f'id="pdf_text_{identifier}" type="#_x0000_t202" '
        f'style="position:absolute;margin-left:{x:.4f}pt;margin-top:{y:.4f}pt;'
        f"width:{width + 2:.4f}pt;height:{height + 2:.4f}pt;z-index:2;"
        "mso-wrap-style:none;mso-position-horizontal-relative:page;"
        'mso-position-vertical-relative:page" filled="f" stroked="f" '
        'o:allowincell="f" o:insetmode="custom">'
        '<v:textbox inset="0pt,0pt,0pt,0pt">'
        '<w:txbxContent xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:p/></w:txbxContent></v:textbox></v:shape>"
    )
    content = shape.find(".//" + qn("w:p"))
    properties = element("w:pPr")
    properties.append(element("w:pStyle", val="NoSpacing"))
    properties.append(
        element("w:spacing", before=0, after=0, line=round(height * 20), lineRule="exact")
    )
    content.append(properties)
    # Preserve individual PDF advances, including tracking and justified spaces.
    # Error diffusion keeps rounding to Word's 1/20 pt units from accumulating.
    groups = []
    rounding_error = 0.0
    for index, char in enumerate(chars):
        natural = font.advance(char["c"]) * size * scale / 100
        advance = (
            chars[index + 1]["origin"][0] - char["origin"][0] if index + 1 < len(chars) else natural
        )
        exact = (advance - natural) * 20 + rounding_error
        tracking = round(exact)
        rounding_error = exact - tracking
        if groups and groups[-1][0] == tracking:
            groups[-1][1] += char["c"]
        else:
            groups.append([tracking, char["c"]])
    for tracking, fragment in groups:
        run = element("w:r")
        properties = element("w:rPr")
        properties.append(element("w:rFonts", ascii=font.family, hAnsi=font.family, cs=font.family))
        if font.bold:
            properties.append(element("w:b"))
        if font.italic:
            properties.append(element("w:i"))
        properties.append(element("w:noProof"))
        properties.append(element("w:color", val=f"{span['color']:06X}"))
        properties.append(element("w:spacing", val=tracking))
        properties.append(element("w:w", val=scale))
        properties.append(element("w:kern", val=32767))
        properties.append(element("w:sz", val=round(size * 2)))
        run.append(properties)
        node = element("w:t")
        node.set(qn("xml:space"), "preserve")
        node.text = fragment
        run.append(node)
        content.append(run)
    picture = element("w:pict")
    # Without a defined textbox type, some readers apply rectangle geometry
    # padding and clip the text even when the textbox insets are zero.
    picture.append(
        parse_xml(
            '<v:shapetype xmlns:v="urn:schemas-microsoft-com:vml" '
            'xmlns:o="urn:schemas-microsoft-com:office:office" '
            'id="_x0000_t202" coordsize="21600,21600" o:spt="202" '
            'path="m,l,21600r21600,l21600,xe"><v:stroke joinstyle="miter"/>'
            '<v:path gradientshapeok="t" o:connecttype="rect"/></v:shapetype>'
        )
    )
    picture.append(shape)
    paragraph.add_run()._r.append(picture)


def precise_docx(pdf, target):
    document = Document()
    document.settings.element.append(element("w:embedTrueTypeFonts"))
    fonts = Fonts(document, pdf)
    image_bytes = 0
    box_count = 0
    for index, page in enumerate(pdf):
        # Work in displayed page coordinates, including rotated/cropped PDFs.
        page.remove_rotation()
        width, height = page.rect.width, page.rect.height
        if min(width, height) < 36 or max(width, height) > 1584:
            raise PDFWordError("This PDF has a page size that Word cannot represent reliably.")
        if width * height * 4 > MAX_PAGE_PIXELS:
            raise PDFWordError("A page exceeds the safe image rendering limit.")
        section = (
            document.sections[0] if not index else document.add_section(WD_SECTION_START.NEW_PAGE)
        )
        section.page_width, section.page_height = Pt(width), Pt(height)
        section.orientation = WD_ORIENT.LANDSCAPE if width > height else WD_ORIENT.PORTRAIT
        section.left_margin = section.right_margin = Pt(0)
        section.top_margin = section.bottom_margin = Pt(0)
        section.header_distance = section.footer_distance = Pt(0)
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_before = paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = Pt(1)
        page_fonts = fonts.for_page(page)
        spans = []
        raw = page.get_text(
            "rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
        )
        for block in raw["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                if line["dir"] != (1, 0) or line["wmode"]:
                    continue
                for span in line["spans"]:
                    font = page_fonts.get(span["font"])
                    characters = "".join(char["c"] for char in span["chars"])
                    values = (*span["bbox"], span["size"])
                    if (
                        not font
                        or not characters.strip()
                        or not font.supports(characters)
                        or span.get("alpha", 255) != 255
                        or not all(math.isfinite(value) for value in values)
                        or not 0.5 <= span["size"] <= 400
                    ):
                        continue
                    spans.append((span, font))
        box_count += len(spans)
        if box_count > MAX_TEXT_BOXES:
            raise PDFWordError(
                "This PDF needs too many text boxes. Split it or use Exact appearance."
            )
        for span, _ in spans:
            rect = pymupdf.Rect(span["bbox"])
            if rect.width <= 0.2:
                continue
            # A thin band removes this text without catching neighboring lines.
            middle = (rect.y0 + rect.y1) / 2
            page.add_redact_annot(
                pymupdf.Rect(rect.x0 + 0.1, middle - 0.05, rect.x1 - 0.1, middle + 0.05),
                fill=False,
                cross_out=False,
            )
        if spans:
            page.apply_redactions(images=0, graphics=0, text=0)
        pixels = page.get_pixmap(dpi=144, alpha=False)
        image = pixels.tobytes("png")
        image_bytes += len(image)
        if image_bytes > MAX_IMAGE_BYTES:
            raise PDFWordError(
                "Rendered pages exceed the 48 MB image budget. Split this PDF first."
            )
        place_page_image(paragraph, image, width, height)
        del pixels, image
        for offset, (span, font) in enumerate(spans):
            text_box(paragraph, span, font, box_count - len(spans) + offset)
    document.save(target)
