"""Deterministic check of requirement wording, ISO/IEC/IEEE 29148:2018 §5.2.7.

The standard lists constructs to avoid in requirement statements: superlatives,
subjective language, ambiguous adverbs and adjectives, loopholes, open-ended terms,
comparative phrases and terms implying totality. Each hit becomes a candidate
finding with an exact quotation. The independent verifier still decides whether
the document resolves it elsewhere, for example with a numeric bound.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from .models import ParsedDocument, ProposedEvidence, QualityCriterion as C, Requirement
from .reviewers import ProposedFinding

CLAUSE = "ISO/IEC/IEEE 29148:2018, п. 5.2.7"
WORD = r"(?<![\w-])(?:{})(?![\w-])"
SENTENCE = re.compile(r"[^.!?;\n]+[.!?;]?")


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    criterion: C
    pattern: re.Pattern[str]
    problem: str
    fix: str


def _terms(*alternatives: str) -> re.Pattern[str]:
    return re.compile(WORD.format("|".join(alternatives)), re.IGNORECASE)


CATEGORIES = (
    Category(
        "subjective", "субъективная формулировка", C.VERIFIABILITY,
        _terms(r"удобн\w*", r"интуитивн\w*", r"понятн\w*", r"дружелюбн\w*", r"красив\w*", r"современн\w*",
               r"user[- ]friendly", r"easy to use", r"intuitive"),
        "Оценка зависит от мнения проверяющего, поэтому у теста нет объективного ожидаемого результата.",
        "Заменить оценку измеримым критерием: действие, условие и наблюдаемый результат.",
    ),
    Category(
        "vague", "неопределённая мера", C.AMBIGUITY,
        _terms(r"быстр(?:о|ый|ая|ое|ые|ого|ым|ом)", r"медленн\w*", r"своевременн\w*", r"оперативн\w*",
               r"значительн\w*", r"существенн\w*", r"достаточн\w*", r"разумн\w*", r"приемлем\w*",
               r"нормальн\w*", r"как можно (?:быстрее|скорее|раньше)", r"в кратчайш\w+ срок\w*",
               r"примерно", r"приблизительно", r"почти", r"обычно", r"как правило", r"в основном",
               r"quickly", r"fast", r"timely", r"significant", r"sufficient", r"reasonable", r"approximately"),
        "Мера не задана числом и единицей, поэтому исполнители и тестировщики выберут разные границы.",
        "Указать числовую границу с единицей измерения и условием, при котором она действует.",
    ),
    Category(
        "loophole", "лазейка", C.VERIFIABILITY,
        _terms(r"по возможности", r"при необходимости", r"по мере необходимости", r"в случае необходимости",
               r"если возможно", r"если применимо", r"по усмотрению", r"при наличии возможности",
               r"if possible", r"as appropriate", r"as applicable", r"if necessary"),
        "Оговорка позволяет не выполнять требование, поэтому его нельзя однозначно принять или отклонить.",
        "Указать конкретное условие, при котором действие обязательно, и поведение в остальных случаях.",
    ),
    Category(
        "open_ended", "открытый перечень", C.COMPLETENESS,
        _terms(r"и т\.\s?д\.?", r"и т\.\s?п\.?", r"и др\.", r"и так далее", r"и тому подобное", r"и прочее",
               r"но не ограничиваясь", r"etc\.?", r"and so on", r"not limited to"),
        "Перечень не закрыт, поэтому неизвестно, какие ещё случаи нужно реализовать и проверить.",
        "Перечислить все элементы явно или задать правило, по которому элемент входит в перечень.",
    ),
    Category(
        "superlative", "превосходная степень", C.VERIFIABILITY,
        _terms(r"наилучш\w*", r"наиболее", r"максимально", r"минимально возможн\w*", r"оптимальн\w*",
               r"самы[йхем]\s+\w+", r"best", r"optimal", r"as much as possible"),
        "Превосходную степень нельзя проверить без эталона сравнения.",
        "Заменить превосходную степень конкретным пороговым значением.",
    ),
    Category(
        "comparative", "сравнение без базы", C.VERIFIABILITY,
        _terms(r"лучше", r"быстрее", r"удобнее", r"эффективнее", r"надёжнее", r"надежнее", r"better than",
               r"faster than"),
        "Сравнение не указывает, с чем сравнивать и насколько, поэтому результат не проверяется.",
        "Указать базу сравнения и требуемую величину улучшения.",
    ),
    Category(
        "totality", "абсолютное утверждение", C.AMBIGUITY,
        _terms(r"всегда", r"никогда", r"во всех случаях", r"ни при каких", r"always", r"never"),
        "Абсолютное утверждение без перечня исключений трудно реализовать и полностью проверить.",
        "Перечислить условия и исключения либо подтвердить, что исключений действительно нет.",
    ),
    Category(
        "pending", "отложенное решение", C.COMPLETENESS,
        _terms(r"TBD", r"TBC", r"TODO", r"будет определ\w+", r"определ\w+ позже", r"уточня\w+ позже",
               r"по согласованию"),
        "Часть требования отложена, поэтому реализацию и тест нельзя завершить.",
        "Принять решение и записать значение или правило в документ.",
    ),
)


def _occurrence(text: str, quote: str, position: int) -> int:
    """1-based occurrence of quote at position, counting overlapping matches as the resolver does."""
    count, start = 0, text.find(quote)
    while 0 <= start <= position:
        count += 1
        start = text.find(quote, start + 1)
    return count


def lint(document: ParsedDocument, requirements: tuple[Requirement, ...]) -> tuple[ProposedFinding, ...]:
    blocks = {block.id: block for block in document.blocks}
    hits: dict[tuple[str, str], list[tuple[str, int, str]]] = defaultdict(list)
    for requirement in requirements:
        span = requirement.source.location
        text = blocks[span.block_id].text
        for sentence in SENTENCE.finditer(text, span.start, span.end):
            quote = sentence[0].strip()
            if not quote:
                continue
            start = sentence.start() + sentence[0].index(quote)
            for category in CATEGORIES:
                terms = sorted({match[0] for match in category.pattern.finditer(quote)})
                if terms:
                    hits[(requirement.id, category.key)].append((quote, start, ", ".join(f"«{t}»" for t in terms)))

    by_key = {category.key: category for category in CATEGORIES}
    owners = {requirement.id: requirement for requirement in requirements}
    findings = []
    for (requirement_id, key), items in hits.items():
        category = by_key[key]
        block = blocks[owners[requirement_id].source.location.block_id]
        evidence = tuple(ProposedEvidence(
            block_id=block.id, quote=quote, occurrence=_occurrence(block.text, quote, start),
            requirement_id=requirement_id,
        ) for quote, start, _ in dict.fromkeys(items))
        terms = "; ".join(dict.fromkeys(term for _, _, term in items))
        findings.append(ProposedFinding(
            criterion=category.criterion,
            subject=f"{category.label.capitalize()} в {requirement_id}",
            explanation=f"Формулировка содержит {terms} ({category.label}, {CLAUSE}). {category.problem}",
            recommendation=category.fix,
            evidence=evidence,
        ))
    return tuple(findings)
