import json
import re
from typing import Literal

from pydantic import Field

from .models import (
    Citation, ContextPassage, CoverageRecord, ParsedDocument, Requirement, Schema, SourceBlock, SourceLocation, Text, UnitKind,
)
from .prompts import EXTRACTION_RULES
from .ports import StructuredLLM, StructuredResponseError
from .source import Anchor, EvidenceError, SourceIndex, contains, stable_id

EXTRACTION_BATCH_CHARS = 5500
HTTP_CONTRACT_FIELDS = {
    "rest type", "http method", "request method", "request url", "curl",
    "http-метод", "метод http", "метод запроса", "url запроса",
}
EMPTY_CONTRACT_VALUES = {"", "-", "–", "—", "n/a", "tbd"}
RESPONSE_HEADING = re.compile(
    r"^(?:(?:HTTP\s+)?[1-5]\d{2}\b|(?:примеры?\s+)?(?:ответ(?:а|ы)?|responses?)\b)", re.IGNORECASE,
)
JSON_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})[ \t]*json\b", re.IGNORECASE)

class Atom(Schema):
    statement: Text
    source: Anchor | None = None
    kind: UnitKind = "atomic_requirement"


class Lines(Schema):
    block_id: Text
    first_line: int = Field(ge=1)
    last_line: int = Field(ge=1)


class Group(Schema):
    source: Lines | Anchor
    original_id: Anchor | None
    atoms: tuple[Atom, ...] = Field(min_length=1)


class Exclusion(Schema):
    block_id: Text
    kind: Literal["supporting_context", "metadata"]
    reason: Text


class ExcludedPassage(Schema):
    source: Anchor
    kind: Literal["supporting_context", "metadata"]
    reason: Text


class Extraction(Schema):
    groups: tuple[Group, ...]
    excluded: tuple[Exclusion, ...] = ()
    excluded_passages: tuple[ExcludedPassage, ...] = ()


class RequirementsExtractor:
    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm

    def extract(self, document: ParsedDocument) -> tuple[Requirement, ...]:
        return self.extract_with_coverage(document)[0]

    def extract_with_coverage(self, document: ParsedDocument) -> tuple[tuple[Requirement, ...], tuple[CoverageRecord, ...]]:
        index = SourceIndex(document)
        units: list[Requirement] = []
        coverage: list[CoverageRecord] = []

        def inventory(blocks: list[SourceBlock]) -> None:
            targets = {b.id for b in blocks}
            payload = {"title": document.title, "blocks": [b.model_dump() for b in blocks]}
            for attempt in range(2):
                try:
                    draft = self.llm.generate(EXTRACTION_RULES, json.dumps(payload, ensure_ascii=False), Extraction)
                    rows = self._ground(document, draft)
                    analyzed = {r.source.location.block_id for r in rows}
                    skipped = {entry.block_id: entry for entry in draft.excluded}
                    if (len(skipped) != len(draft.excluded) or analyzed.intersection(skipped)
                            or analyzed.union(skipped) != targets):
                        missing = targets - analyzed - skipped.keys()
                        raise EvidenceError(f"Missing or inconsistent block classification: {sorted(missing)}")
                    context: dict[str, list[ContextPassage]] = {}
                    for passage in draft.excluded_passages:
                        citation = index.resolve(passage.source)
                        span = citation.location
                        if span.block_id not in analyzed or any(
                            r.source.location.block_id == span.block_id and
                            r.source.location.start < span.end and span.start < r.source.location.end for r in rows
                        ):
                            raise EvidenceError("Context exclusions must be separate from extracted units in the same block")
                        context.setdefault(span.block_id, []).append(ContextPassage(
                            source=citation, kind=passage.kind, reason=passage.reason))
                    for block in blocks:
                        exclusion = skipped.get(block.id)
                        block_is_metadata = exclusion is not None and exclusion.kind == "metadata"
                        self._check_table_rows(block, rows, context.get(block.id, []), block_is_metadata)
                        if (block_is_metadata and block.section and RESPONSE_HEADING.match(block.section[-1])
                                and JSON_FENCE.match(block.text)):
                            raise EvidenceError(f"API response example in {block.id} carries a specification fact")
                    break
                except (EvidenceError, StructuredResponseError) as exc:
                    if attempt:
                        if len(blocks) == 1:
                            raise
                        midpoint = len(blocks) // 2
                        inventory(blocks[:midpoint])
                        inventory(blocks[midpoint:])
                        return
                    payload["previous_validation_error"] = str(exc)
            units.extend(rows)
            for block in blocks:
                coverage.append(CoverageRecord(**skipped[block.id].model_dump()) if block.id in skipped
                                else CoverageRecord(block_id=block.id, kind="analyzed", reason="Есть привязанные единицы анализа.",
                                                    context_passages=tuple(context.get(block.id, ()))))

        batch: list[SourceBlock] = []
        size = 0
        for block in document.blocks:
            if batch and size + len(block.text) > EXTRACTION_BATCH_CHARS:
                inventory(batch)
                batch, size = [], 0
            batch.append(block)
            size += len(block.text)
        inventory(batch)
        ordered = sorted(units, key=lambda r: (index.positions[r.source.location.block_id], r.source.location.start,
                                               r.source.location.end))
        return tuple(r.model_copy(update={"id": f"R-{i:03}"}) for i, r in enumerate(ordered, 1)), tuple(coverage)

    @staticmethod
    def _check_table_rows(block: SourceBlock, rows: tuple[Requirement, ...], context: list[ContextPassage],
                          block_is_metadata: bool) -> None:
        spans = [r.source.location for r in rows if r.source.location.block_id == block.id]
        spans.extend(item.source.location for item in context)
        lines = block.text.splitlines(keepends=True)
        if len(lines) < 3 or "|" not in lines[1] or lines[1].strip(" |:-\r\n\t"):
            return
        metadata_spans = [item.source.location for item in context if item.kind == "metadata"]
        start = len(lines[0]) + len(lines[1])
        for line in lines[2:]:
            end = start + len(line)
            if spans and line.strip(" |\r\n\t") and not any(s.start < end and start < s.end for s in spans):
                raise EvidenceError(f"Uncovered table row in {block.id}: {line.strip()}")
            if block_is_metadata or any(s.start < end and start < s.end for s in metadata_spans):
                label, _, value = line.strip().strip("|").partition("|")
                if (label.strip(" *_`\t").casefold() in HTTP_CONTRACT_FIELDS
                        and value.strip(" *_`\t").casefold() not in EMPTY_CONTRACT_VALUES):
                    raise EvidenceError(f"API contract row in {block.id} carries a specification fact: {line.strip()}")
            start = end

    def _ground(self, document: ParsedDocument, draft: Extraction) -> tuple[Requirement, ...]:
        index = SourceIndex(document)
        requirements: list[Requirement] = []
        seen: set[str] = set()
        for group in draft.groups:
            if isinstance(group.source, Lines):
                block = index.blocks.get(group.source.block_id)
                lines = block.text.splitlines(keepends=True) if block else []
                first, last = group.source.first_line, group.source.last_line
                if not first <= last <= len(lines):
                    raise EvidenceError("Invalid group line range")
                start = sum(map(len, lines[:first - 1]))
                quote = "".join(lines[first - 1:last]).rstrip("\r\n")
                if not quote.strip():
                    raise EvidenceError("Empty group line range")
                parent = Citation(quote=quote, location=SourceLocation(block_id=group.source.block_id,
                                  start=start, end=start + len(quote)))
            else:
                parent = index.resolve(group.source)
            group_id = stable_id("G", document.id, parent.location.model_dump())
            label = index.resolve(group.original_id) if group.original_id else None
            if label is not None:
                index.verify_label(label, parent)
            for atom in group.atoms:
                source = index.resolve(atom.source, within=parent.location) if atom.source else parent
                if not contains(parent.location, source.location):
                    raise EvidenceError("Атомарное требование находится вне исходного пункта")
                req_id = stable_id("R", document.id, source.location.model_dump())
                if req_id in seen:
                    raise EvidenceError("Повторная привязка требования к той же позиции")
                seen.add(req_id)
                requirements.append(Requirement(
                    id=req_id, original_id=label, statement=atom.statement, source=source, kind=atom.kind,
                    group_id=group_id, group_source=parent,
                    section=index.blocks[source.location.block_id].section,
                ))
        return tuple(requirements)
