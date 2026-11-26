import logging
import re
from hashlib import sha256
from pathlib import Path

import pymupdf
from markdown_it import MarkdownIt

from .models import ParsedDocument, SourceBlock

LOG = logging.getLogger(__name__)


class DocumentParseError(ValueError):
    pass


class MarkdownParser:
    def parse(self, path: Path) -> ParsedDocument:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentParseError("Markdown must be encoded as UTF-8.") from exc
        # Match markdown-it's line boundaries while slicing the original text.
        offsets = [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", text)), len(text)]
        tokens = MarkdownIt("commonmark").enable("table").parse(text)
        blocks: list[SourceBlock] = []
        headings: list[tuple[int, str]] = []
        title: str | None = None
        first_heading: str | None = None

        def add(start: int, end: int) -> None:
            source = text[offsets[start]:offsets[end]]
            if source.strip():
                blocks.append(SourceBlock(
                    id=f"L{start + 1}-L{end}", text=source, section=tuple(h for _, h in headings)
                ))

        cursor = 0
        for index, token in enumerate(tokens):
            if token.level != 0 or not token.block or token.map is None:
                continue
            start, end = token.map
            if start < cursor:
                continue
            add(cursor, start)  # Retain definitions and other source omitted by tokenization.
            if token.type == "heading_open":
                level = int(token.tag[1:])
                heading = tokens[index + 1].content.strip()
                headings = [(depth, label) for depth, label in headings if depth < level]
                if heading:
                    headings.append((level, heading))
                    first_heading = first_heading or heading
                    if level == 1:
                        title = title or heading
            add(start, end)
            cursor = end
        add(cursor, len(offsets) - 1)
        if not blocks:
            raise DocumentParseError("Markdown contains no text.")
        return ParsedDocument(id=sha256(raw).hexdigest(), title=title or first_heading or path.name,
                              blocks=tuple(blocks))


class PdfParser:
    def parse(self, path: Path) -> ParsedDocument:
        raw = path.read_bytes()
        blocks: list[SourceBlock] = []
        try:
            with pymupdf.open(stream=raw, filetype="pdf") as pdf:
                if not pdf.is_pdf:
                    raise DocumentParseError("Input is not a PDF document.")
                if pdf.needs_pass:
                    raise DocumentParseError("Password-protected PDFs are unsupported.")
                title = (pdf.metadata.get("title") or "").strip() or path.name
                outline = sorted((page, i, level, heading.strip())
                                 for i, (level, heading, page) in enumerate(pdf.get_toc()) if page > 0)
                headings: list[tuple[int, str]] = []
                next_heading = 0
                for number, page in enumerate(pdf, start=1):
                    while next_heading < len(outline) and outline[next_heading][0] <= number:
                        _, _, level, heading = outline[next_heading]
                        headings = [(depth, label) for depth, label in headings if depth < level]
                        if heading:
                            headings.append((level, heading))
                        next_heading += 1
                    text = page.get_text("text", sort=True)
                    if text.strip():
                        blocks.append(SourceBlock(id=f"P{number}", page=number, text=text,
                                                  section=tuple(h for _, h in headings)))
                    elif page.get_image_info():
                        LOG.warning("PDF page %d contains images without selectable text and was skipped.", number)
        except (pymupdf.FileDataError, pymupdf.EmptyFileError, RuntimeError) as exc:
            raise DocumentParseError("Could not read the PDF. The file may be damaged.") from exc
        if not blocks:
            raise DocumentParseError("PDF has no selectable text. Run OCR before analysis.")
        return ParsedDocument(id=sha256(raw).hexdigest(), title=title, blocks=tuple(blocks))


class FileDocumentParser:
    def parse(self, path: Path) -> ParsedDocument:
        match path.suffix.lower():
            case ".md" | ".markdown":
                return MarkdownParser().parse(path)
            case ".pdf":
                return PdfParser().parse(path)
            case _:
                raise DocumentParseError("Unsupported input format. Use .md, .markdown or .pdf.")
