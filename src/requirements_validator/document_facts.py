"""Document properties measured from the source without the model.

The whole-document review receives these metrics as pointers. The model still
judges each property and must cite the source verbatim.
"""

import re
from collections import defaultdict

from .models import DocumentMetric, ParsedDocument, QualityCriterion as C, Requirement

MAX_BLOCKS = 15
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
PRIORITY = re.compile(
    r"приоритет|priority|moscow|\b(?:must|should|could|won'?t)[- ]have\b|стабильност|stability|\bP[0-4]\b",
    re.IGNORECASE,
)
PLACEHOLDER = re.compile(
    r"заполня\w*\s+(?:тестировщик|аналитик|позднее|позже)|\bTBD\b|\bTODO\b|не\s+заполнен|"
    # A dash alone is a placeholder in a two-column field/value row, not in a wider data table.
    r"Да\s*/\s*Нет|\[\s\]|^\|(?:\s*\|)+\s*$|^\|[^|\n]+\|\s*[—–]\s*\|\s*$|:\s*[—–]\s*(?:$|\|)",
    re.IGNORECASE | re.MULTILINE,
)
GLOSSARY = re.compile(r"глоссари|термин|определени|сокращени|glossary|definition|abbreviation", re.IGNORECASE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
MARKUP = re.compile(r"[*_`#>|\[\]]+")


def _limited(block_ids: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(block_ids))[:MAX_BLOCKS]


def _bare_id(quote: str) -> str:
    return MARKUP.sub("", quote).strip(" .:;")


def _normalized(sentence: str) -> str:
    return " ".join(MARKUP.sub(" ", sentence).casefold().split()).rstrip(".!?;:")


def _empty_sections(document: ParsedDocument) -> list[str]:
    blocks = document.blocks
    empty = []
    for position, block in enumerate(blocks):
        heading = HEADING.match(block.text.strip())
        if heading is None:
            continue
        level = len(heading[1])
        following = blocks[position + 1].text.strip() if position + 1 < len(blocks) else ""
        nested = HEADING.match(following)
        if not following or re.fullmatch(r"[-*_]{3,}", following) or (nested and len(nested[1]) <= level):
            empty.append(block.id)
    return empty


def measure(document: ParsedDocument, requirements: tuple[Requirement, ...]) -> tuple[DocumentMetric, ...]:
    metrics: list[DocumentMetric] = []
    order = {block.id: position for position, block in enumerate(document.blocks)}

    def by_order(block_ids: list[str]) -> tuple[str, ...]:
        return _limited(sorted(block_ids, key=lambda block_id: order.get(block_id, len(order))))

    # IEEE 830 §4.3.8: each requirement needs a unique name or reference number.
    total = len(requirements)
    unnamed = [r.source.location.block_id for r in requirements if r.original_id is None]
    metrics.append(DocumentMetric(
        criterion=C.TRACEABILITY, name="Элементы спецификации с собственным идентификатором",
        value=f"{total - len(unnamed)} из {total}", block_ids=by_order(unnamed),
    ))
    groups_by_id: dict[str, set[str]] = defaultdict(set)
    for requirement in requirements:
        if requirement.original_id is not None:
            groups_by_id[_bare_id(requirement.original_id.quote)].add(requirement.group_id)
    repeated = sorted(label for label, groups in groups_by_id.items() if label and len(groups) > 1)
    metrics.append(DocumentMetric(
        criterion=C.TRACEABILITY, name="Повторяющиеся идентификаторы",
        value=", ".join(repeated) if repeated else "не найдены",
    ))
    acceptance = [r for r in requirements if r.kind == "acceptance_criterion"]
    labels = [label for label in groups_by_id if label]
    if acceptance:
        linked = [r for r in acceptance if any(label in r.source.quote for label in labels)]
        metrics.append(DocumentMetric(
            criterion=C.TRACEABILITY, name="Критерии приёмки со ссылкой на идентификатор требования",
            value=f"{len(linked)} из {len(acceptance)}",
            block_ids=by_order([r.source.location.block_id for r in acceptance if r not in linked]),
        ))

    # IEEE 830 §4.3.5: importance and/or stability of each requirement.
    ranked = [block.id for block in document.blocks if PRIORITY.search(block.text)]
    metrics.append(DocumentMetric(
        criterion=C.RANKING, name="Маркеры приоритета или стабильности",
        value=f"найдены в блоках: {len(ranked)}" if ranked else "не найдены", block_ids=by_order(ranked),
    ))

    # ISO 29148 §5.2.5 Conforming: the document's own template is filled in.
    placeholders = [block.id for block in document.blocks if PLACEHOLDER.search(block.text)]
    metrics.append(DocumentMetric(
        criterion=C.CONFORMANCE, name="Блоки с незаполненными полями шаблона",
        value=str(len(placeholders)), block_ids=by_order(placeholders),
    ))
    empty = _empty_sections(document)
    metrics.append(DocumentMetric(
        criterion=C.CONFORMANCE, name="Заголовки разделов без содержимого",
        value=str(len(empty)), block_ids=by_order(empty),
    ))

    # IEEE 830 §4.3.7: a requirement should not appear in more than one place.
    seen: dict[str, set[str]] = defaultdict(set)
    for block in document.blocks:
        if HEADING.match(block.text.strip()):
            continue
        # Remove markup first so a bold label such as **Ф-1.** splits off as its own short sentence.
        for sentence in SENTENCE_END.split(MARKUP.sub(" ", block.text)):
            normalized = _normalized(sentence)
            if len(normalized.split()) >= 4:
                seen[normalized].add(block.id)
    duplicated = [block_id for blocks in seen.values() if len(blocks) > 1 for block_id in blocks]
    metrics.append(DocumentMetric(
        criterion=C.MODIFIABILITY, name="Дословно повторяющиеся предложения",
        value=str(sum(len(blocks) > 1 for blocks in seen.values())), block_ids=by_order(duplicated),
    ))

    # ISO 29148 §5.2.6 Comprehensible: terms are defined.
    glossary = [block.id for block in document.blocks
                if any(GLOSSARY.search(heading) for heading in block.section)]
    metrics.append(DocumentMetric(
        criterion=C.COMPREHENSIBILITY, name="Глоссарий или раздел определений",
        value="есть" if glossary else "отсутствует", block_ids=by_order(glossary),
    ))

    # ISO 29148 §5.2.6 Able to be validated / IEEE 830 §4.3.6 Verifiable.
    metrics.append(DocumentMetric(
        criterion=C.VALIDATABILITY, name="Критерии приёмки среди элементов спецификации",
        value=f"{len(acceptance)} из {total}",
        block_ids=by_order([r.source.location.block_id for r in acceptance]),
    ))
    return tuple(metrics)
