"""Переносит структуру HTML-шаблона в редактируемые таблицы и абзацы Word."""

from pathlib import Path
import re
from uuid import uuid4
from xml.etree.ElementTree import Element, fromstring

from docx import Document
from docx.document import Document as WordDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from docx.text.paragraph import Paragraph
from lxml.etree import tostring

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_NAME = "PT Sans"
WIDTH = Mm(174)
PALETTE = {
    "critical": ("FCEBEB", "791F1F"),
    "major": ("FAEEDA", "633806"),
    "minor": ("F1EFE8", "444441"),
    "clear": ("EAF3DE", "27500A"),
    "neutral": ("F2F5F8", "182435"),
}
PARAGRAPH_STYLES = {
    "h1": "Title", "h2": "Heading 1", "h3": "Heading 2", "h4": "Heading 3",
    "p": "Normal", "blockquote": "Quote",
}
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _xml(parent, tag: str, **attributes: str):
    element = OxmlElement(f"w:{tag}")
    for name, value in attributes.items():
        element.set(qn(f"w:{name}"), value)
    parent.append(element)
    return element


def _colors(element: Element) -> tuple[str, str] | None:
    return next((PALETTE[name] for name in element.get("class", "").split() if name in PALETTE), None)


def _inline(paragraph: Paragraph, element: Element, *, bold: bool = False, color: str | None = None) -> None:
    if element.text:
        run = paragraph.add_run(element.text)
        run.bold = True if bold else None
        if color:
            run.font.color.rgb = RGBColor.from_string(color)
    for child in element:
        if child.tag == "span" and (colors := _colors(child)):
            background, foreground = colors
            run = paragraph.add_run(f" {child.text} ")
            run.bold = True
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor.from_string(foreground)
            _xml(run._r.get_or_add_rPr(), "shd", fill=background)
        else:
            _inline(paragraph, child, bold=bold or child.tag in {"b", "strong"}, color=color)
        if child.tail and child.tail.strip():
            run = paragraph.add_run(child.tail)
            if color:
                run.font.color.rgb = RGBColor.from_string(color)


def _table(document: WordDocument, element: Element) -> None:
    if (caption := element.find("caption")) is not None:
        _blocks(document, list(caption))
    column_elements = element.findall("colgroup/col")
    column_count = len(column_elements) or len(element.findall("tbody/tr/td"))
    widths = [int(col.get("style").removeprefix("width: ").removesuffix("%")) / 100
              for col in column_elements] or [1 / column_count] * column_count
    table = document.add_table(rows=0, cols=column_count)
    table.autofit = False
    for column, width in zip(table.columns, widths, strict=True):
        column.width = int(WIDTH * width)
    _xml(table._tbl.tblPr, "tblInd", w="108", type="dxa")
    borders = _xml(table._tbl.tblPr, "tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        _xml(borders, edge, val="single", color="CFD6DF", sz="4")

    for group in element.findall("thead") + element.findall("tbody"):
        for row_element in group:
            row = table.add_row()
            _xml(row._tr.get_or_add_trPr(), "cantSplit")
            if group.tag == "thead":
                _xml(row._tr.get_or_add_trPr(), "tblHeader")
            for index, (cell, node) in enumerate(zip(row.cells, row_element)):
                if node.get("colspan"):
                    cell = cell.merge(row.cells[index + int(node.get("colspan")) - 1])
                colors = _colors(node) or (PALETTE["neutral"] if node.tag == "th" else None)
                if colors:
                    _xml(cell._tc.get_or_add_tcPr(), "shd", fill=colors[0])
                for position, content in enumerate(node.findall("p") or [node]):
                    paragraph = cell.paragraphs[0] if position == 0 else cell.add_paragraph()
                    paragraph.paragraph_format.space_before = Pt(2)
                    paragraph.paragraph_format.space_after = Pt(2)
                    if element.get("class") == "metrics":
                        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    if content.get("class") == "quote":
                        paragraph.style = "Quote"
                    _inline(paragraph, content, bold=node.tag == "th", color=colors[1] if colors else None)
                    if element.get("class") == "metrics":
                        for run in paragraph.runs:
                            if run.bold:
                                run.font.size = Pt(20)


def _embed_fonts(document: WordDocument) -> None:
    font_table = document.part.part_related_by(RT.FONT_TABLE)
    fonts = parse_xml(font_table.blob)
    family = _xml(fonts, "font", name=FONT_NAME)
    for tag, weight in (("embedRegular", "Regular"), ("embedBold", "Bold")):
        key = uuid4()
        font = bytearray((FONT_DIR / f"PTSans-{weight}.ttf").read_bytes())
        # OOXML obfuscates the first 32 bytes with the reversed font key.
        for index in range(32):
            font[index] ^= key.bytes[15 - index % 16]
        part = Part(
            PackURI(f"/word/fonts/PTSans-{weight}.odttf"),
            "application/vnd.openxmlformats-officedocument.obfuscatedFont",
            bytes(font), document.part.package,
        )
        entry = _xml(family, tag, fontKey="{" + str(key).upper() + "}")
        entry.set(qn("r:id"), font_table.relate_to(part, RT.FONT))
    font_table._blob = tostring(fonts, encoding="UTF-8", xml_declaration=True, standalone=True)
    _xml(document.settings.element, "embedTrueTypeFonts", val="true")


def _configure(document: WordDocument, title: str) -> None:
    document.core_properties.title = title
    document.core_properties.author = ""
    document.core_properties.language = "ru"
    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Mm(18)
    section.footer_distance = Mm(8)
    sizes = {"Title": 22, "Heading 1": 14, "Heading 2": 11, "Heading 3": 10, "Quote": 9, "Footer": 9}
    for name in (*sizes, "Normal", "List Number", "List Bullet"):
        style = document.styles[name]
        style.font.name = FONT_NAME
        style.font.size = Pt(sizes.get(name, 10))
        style.font.color.rgb = RGBColor.from_string("455264" if name in {"Quote", "Footer"} else "182435")
        style.font.italic = False
        style.paragraph_format.space_after = Pt(4)
        fonts = style.element.get_or_add_rPr().rFonts
        for attribute in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(qn(f"w:{attribute}"), None)
    title_style = document.styles["Title"]
    title_style.font.bold = True
    properties = title_style.element.get_or_add_pPr()
    if (border := properties.find(qn("w:pBdr"))) is not None:
        properties.remove(border)
    document.styles["Footer"].paragraph_format.tab_stops.clear_all()
    numbering = document.part.numbering_part.element
    for level in numbering.findall(f".//{qn('w:lvl')}"):
        if level.find(qn("w:numFmt")).get(qn("w:val")) == "bullet":
            level.find(qn("w:lvlText")).set(qn("w:val"), "•")
            fonts = level.find(f"{qn('w:rPr')}/{qn('w:rFonts')}")
            fonts.set(qn("w:ascii"), FONT_NAME)
            fonts.set(qn("w:hAnsi"), FONT_NAME)
    footer = section.footer.paragraphs[0]
    footer.paragraph_format.tab_stops.add_tab_stop(WIDTH, WD_TAB_ALIGNMENT.RIGHT)
    footer.add_run(f"{title}\tСтр. ")
    _xml(footer._p, "fldSimple", instr=" PAGE ")
    footer.add_run(" из ")
    _xml(footer._p, "fldSimple", instr=" NUMPAGES ")
    _embed_fonts(document)


def _blocks(document: WordDocument, elements: list[Element]) -> None:
    for element in elements:
        if element.tag == "table":
            if element.get("class") == "metrics":
                spacing = document.add_paragraph().paragraph_format
                spacing.line_spacing = Pt(4)
                spacing.space_after = Pt(0)
            _table(document, element)
        elif element.tag in PARAGRAPH_STYLES:
            paragraph = document.add_paragraph(style=PARAGRAPH_STYLES[element.tag])
            if element.get("class") == "quote":
                paragraph.style = "Quote"
            _inline(paragraph, element)
        elif element.tag in {"ol", "ul"}:
            for item in element:
                paragraph = document.add_paragraph(style="List Number" if element.tag == "ol" else "List Bullet")
                if element.get("class") == "critical-list":
                    background, foreground = PALETTE["critical"]
                    _xml(paragraph._p.get_or_add_pPr(), "shd", fill=background)
                    _inline(paragraph, item, color=foreground)
                else:
                    _inline(paragraph, item)
        elif element.tag == "div" and (colors := _colors(element)):
            for item in element:
                paragraph = document.add_paragraph()
                _xml(paragraph._p.get_or_add_pPr(), "shd", fill=colors[0])
                paragraph.paragraph_format.keep_with_next = item is not element[-1]
                _inline(paragraph, item, color=colors[1])
        else:
            start = len(document.paragraphs)
            _blocks(document, list(element))
            if element.tag == "article":
                card = document.paragraphs[start:]
                for paragraph in card:
                    paragraph.paragraph_format.keep_with_next = paragraph is not card[-1]


def write_docx(html: str, destination: Path) -> None:
    root = fromstring(INVALID_XML.sub("�", html))
    document = Document()
    _configure(document, root.findtext("head/title"))
    _blocks(document, list(root.find("body")))
    document.save(destination)
