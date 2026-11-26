import hashlib
import json
from collections.abc import Mapping

from .models import Anchor, Citation, EvidenceReference, ParsedDocument, Requirement, SourceLocation


class EvidenceError(ValueError):
    """Proposed source reference cannot be established."""


def stable_id(prefix: str, *parts: object) -> str:
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def contains(outer: SourceLocation, inner: SourceLocation) -> bool:
    return (outer.block_id == inner.block_id and outer.start <= inner.start
            and inner.end <= outer.end)


def verify_evidence_owner(ref: EvidenceReference, requirements: Mapping[str, Requirement]) -> None:
    if ref.requirement_id is None:
        return
    owner = requirements.get(ref.requirement_id)
    if owner is None or not contains(owner.source.location, ref.location):
        raise EvidenceError(f"Цитата не относится к требованию {ref.requirement_id}")


class SourceIndex:
    def __init__(self, document: ParsedDocument) -> None:
        self.blocks = {b.id: b for b in document.blocks}
        self.positions = {b.id: i for i, b in enumerate(document.blocks)}

    def verify(self, citation: Citation) -> None:
        span = citation.location
        block = self.blocks.get(span.block_id)
        if (block is None or span.end > len(block.text)
                or block.text[span.start:span.end] != citation.quote):
            raise EvidenceError(f"Цитата не соответствует указанной позиции в блоке {span.block_id}")

    def verify_label(self, label: Citation, clause: Citation) -> None:
        """Check a resolved ID is in its clause, introduces it, or is inherited."""
        span, parent = label.location, clause.location
        if contains(parent, span):
            return
        block = self.blocks[parent.block_id]
        origin = self.blocks[span.block_id]
        if origin.id == block.id:
            gap = block.text[span.end:parent.start]
            if span.end <= parent.start and not any(ch.isalnum() for ch in gap):
                return
        elif (self.positions[origin.id] < self.positions[block.id]
              and block.section[:len(origin.section)] == origin.section):
            if origin.section and label.quote in origin.section[-1]:
                return
            if self.positions[origin.id] + 1 == self.positions[block.id]:
                gap = origin.text[span.end:] + block.text[:parent.start]
                if not any(ch.isalnum() for ch in gap):
                    return
        raise EvidenceError("Идентификатор не относится к исходному пункту")

    def verify_requirement(self, requirement: Requirement) -> None:
        self.verify(requirement.source)
        self.verify(requirement.group_source)
        if not contains(requirement.group_source.location, requirement.source.location):
            raise EvidenceError("Атомарное требование находится вне исходного пункта")
        if requirement.section != self.blocks[requirement.source.location.block_id].section:
            raise EvidenceError("Раздел требования не соответствует исходному тексту")
        if requirement.original_id is not None:
            self.verify(requirement.original_id)
            self.verify_label(requirement.original_id, requirement.group_source)

    def resolve(self, anchor: Anchor, *, within: SourceLocation | None = None) -> Citation:
        block = self.blocks.get(anchor.block_id)
        if block is None:
            raise EvidenceError(f"Неизвестный блок {anchor.block_id}")
        if within and within.block_id == block.id and anchor.occurrence is None:
            start = block.text.find(anchor.quote, within.start, within.end)
            if start >= 0 and block.text.find(anchor.quote, start + 1, within.end) < 0:
                return Citation(quote=anchor.quote, location=SourceLocation(
                    block_id=block.id, start=start, end=start + len(anchor.quote)))
        start = block.text.find(anchor.quote)
        if start >= 0 and anchor.occurrence is None and block.text.find(anchor.quote, start + 1) >= 0:
            raise EvidenceError(f"Неоднозначная позиция цитаты в блоке {block.id}")
        for _ in range(1, anchor.occurrence or 1):
            if start < 0:
                break
            start = block.text.find(anchor.quote, start + 1)
        if start < 0:
            raise EvidenceError(f"Точная цитата не найдена в блоке {block.id}")
        return Citation(quote=anchor.quote, location=SourceLocation(
            block_id=block.id, start=start, end=start + len(anchor.quote)
        ))
