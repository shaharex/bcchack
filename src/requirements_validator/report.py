import os
import re
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile

from jinja2 import Environment, PackageLoader, StrictUndefined

from .models import (
    AnalysisRunResult, Citation, CriterionEvaluation, Finding, QualityCriterion as Criterion, Requirement,
    SeverityDecision, SourceLocation,
)
from .source import SourceIndex
from .standards import compliance_rows

CRITERIA = {
    Criterion.AMBIGUITY: "Однозначность",
    Criterion.VERIFIABILITY: "Проверяемость",
    Criterion.COMPLETENESS: "Полнота",
    Criterion.ATOMICITY: "Атомарность",
    Criterion.CONSISTENCY: "Непротиворечивость",
    Criterion.FEASIBILITY: "Выполнимость",
    Criterion.IMPLEMENTATION_CONSTRAINT: "Обоснованность ограничений реализации",
    Criterion.NECESSITY: "Необходимость",
    Criterion.CORRECTNESS: "Корректность",
    Criterion.CONFORMANCE: "Соответствие шаблону",
    Criterion.COMPREHENSIBILITY: "Понятность",
    Criterion.VALIDATABILITY: "Возможность валидации",
    Criterion.RANKING: "Ранжированность по важности и стабильности",
    Criterion.MODIFIABILITY: "Модифицируемость",
    Criterion.TRACEABILITY: "Прослеживаемость",
}
EXECUTION_STATUSES = {
    "pass": "Совпадает", "conflict": "Противоречие правил", "mismatch": "Расхождение с примером",
    "missing_input": "Не хватает данных", "error": "Не выполнен",
}
COMPLIANCE_STATUSES = {
    "pass": "Соответствует", "fail": "Не соответствует", "not_assessable": "Нельзя оценить",
    "not_run": "Не проверялось",
}
OUTCOMES = {
    "pass": "Дефектов не выявлено",
    "fail": "Обнаружен дефект",
    "not_assessable": "Недостаточно оснований для вывода",
    "not_applicable": "Не применимо",
    "not_checked": "Не проверено",
}
LEVELS = {"critical": "Критическая", "high": "Высокая", "medium": "Средняя", "low": "Низкая"}
KINDS = {
    "atomic_requirement": "Атомарное требование",
    "compound_requirement": "Составное требование",
    "specification_fact": "Факт спецификации",
    "acceptance_criterion": "Критерий приёмки",
}
COVERAGE_KINDS = {
    "analyzed": "Проанализирован",
    "supporting_context": "Вспомогательный контекст",
    "metadata": "Метаданные",
}
REJECTED_STATUSES = {
    "unsupported": "Не подтверждено источником",
    "potential": "Потенциальная проблема",
    "improvement": "Предложение по улучшению",
}
REQUIREMENT_KINDS = {"atomic_requirement", "compound_requirement"}
CHECKLIST_SECTION = re.compile(r"^(?:чек[-\s]?лист|checklist)\b", re.I)
REVIEW_SECTION = re.compile(r"ревью|проверка|review", re.I)
UNFILLED_CHOICES = re.compile(r"\bда\s*/\s*нет\b|\byes\s*/\s*no\b", re.I)


def inline(text: str) -> str:
    escaped = escape(" ".join(text.splitlines()), quote=False)
    escaped = re.sub(r"([\\`*_[\]{}()!#~+|])", r"\\\1", escaped)
    return re.sub(r"^(\d*)([.-])(?=\s)", r"\1\\\2", escaped)


def quotation(text: str) -> str:
    width = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, width + 1)
    newline = "" if text.endswith(("\n", "\r")) else "\n"
    return f"{fence}\n{text}{newline}{fence}"


def report_readiness(result: AnalysisRunResult, *, has_blocking_findings: bool) -> tuple[bool, str]:
    if has_blocking_findings:
        return False, "Рекомендуется отложить реализацию и тестирование затронутых сценариев до устранения блокирующих дефектов."
    if not result.requirements:
        return False, "Готовность не установлена. Проверьте полноту извлечения и наличие требований."
    if result.review.findings:
        return True, "Уточните замечания перед реализацией и тестированием затронутых требований."
    if result.review.rejected or any(e.outcome in {"not_assessable", "not_checked"} for e in result.review.evaluations):
        return False, "Подтверждённых дефектов нет, но вывод о готовности ограничен: остались непроверяемые критерии или неподтверждённые замечания."
    return True, "В пределах проанализированного материала подтверждённых препятствий для начала реализации и тестирования не выявлено."


def normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def unit_subject(unit: Requirement) -> str:
    source = " ".join(unit.source.quote.split())
    statement = " ".join(unit.statement.split())
    if not re.search(r"[А-Яа-яЁё]", statement) and re.search(r"[А-Яа-яЁё]", source):
        statement = source
    return statement if len(statement) <= 125 else statement[:124].rsplit(" ", 1)[0] + "…"


def unit_kind(unit: Requirement) -> str:
    if (any(CHECKLIST_SECTION.search(heading) for heading in unit.section) or
            (any(REVIEW_SECTION.search(heading) for heading in unit.section)
             and UNFILLED_CHOICES.search(unit.source.quote))):
        return "checklist"
    return unit.kind


def finding_consequence(finding: Finding, decision: SeverityDecision | None) -> str | None:
    scenario = finding.scenario
    if scenario:
        # Relation reviews store their consequence after the three scenario fields.
        prefix = " ".join((scenario.description, scenario.expected_a, scenario.expected_b)) + " "
        if finding.explanation.startswith(prefix):
            return finding.explanation.removeprefix(prefix).strip() or None
    impact = finding.impact or (decision.rationale if decision else None)
    if impact is None:
        return None
    comparable = normalized(impact)
    repeated = {normalized(finding.explanation), "подтверждено"}
    if scenario:
        repeated.update(normalized(text) for text in (
            scenario.description, scenario.expected_a, scenario.expected_b
        ))
    return None if comparable.rstrip(".!") in {text.rstrip(".!") for text in repeated} else impact


@dataclass(frozen=True)
class FindingCard:
    finding: Finding
    label: str
    level: str | None
    consequence: str | None
    evidence: dict[SourceLocation, tuple[Citation, list[str]]]

    @property
    def anchor(self) -> str:
        return f"defect-{self.label.lower()}"

    @property
    def blocking(self) -> bool:
        return self.finding.impact_class == "blocks_behavior" or self.level == "critical"


def write_text_atomic(destination: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=destination.parent,
                                prefix=".requirements-report-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class MarkdownReportGenerator:
    def __init__(self, *, diagnostics: bool = False) -> None:
        env = Environment(loader=PackageLoader("requirements_validator"), undefined=StrictUndefined,
                          trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
        env.filters.update(md=inline, quotation=quotation)
        self._template = env.get_template("report.md.j2")
        self.diagnostics = diagnostics

    def render(self, result: AnalysisRunResult, *, rendered_at: datetime | None = None) -> str:
        result.check_references()
        index = SourceIndex(result.document)
        line_breaks = {block.id: [match.end() for match in re.finditer(r"\r\n|\r|\n", block.text)]
                       for block in result.document.blocks}
        anchors = {unit.id: f"unit-{i}" for i, unit in enumerate(result.requirements, 1)}

        def location(citation: Citation, *, detailed: bool = False) -> str:
            span = citation.location
            block = index.blocks[span.block_id]
            section = list(block.section)
            if section and section[0] == result.document.title:
                section.pop(0)
            place = ""
            if block.page is not None:
                place = f"стр. {block.page}"
            elif lines := re.fullmatch(r"L(\d+)-L(\d+)", block.id):
                base = int(lines[1])
                first = base + bisect_right(line_breaks[block.id], span.start)
                last = base + bisect_right(line_breaks[block.id], span.end - 1)
                if last > int(lines[2]):
                    place = f"блок {block.id}"
                else:
                    place = f"строка {first}" if first == last else f"строки {first}–{last}"
            else:
                place = f"блок {block.id}"
            locality = " → ".join(section) if detailed else (section[-1] if section else "")
            coordinates = f" · {block.id}, символы [{span.start}, {span.end})" if detailed else ""
            return " · ".join(filter(None, (locality, place))) + coordinates

        evaluations: dict[str, list[CriterionEvaluation]] = defaultdict(list)
        for evaluation in result.review.evaluations:
            evaluations[evaluation.requirement_id].append(evaluation)
        decisions = {decision.finding_id: decision for decision in result.severities or ()}
        counts = Counter(decision.level for decision in decisions.values())
        outcome_counts = Counter(e.outcome for e in result.review.evaluations)
        unfinished = outcome_counts["not_assessable"] + outcome_counts["not_checked"]
        unit_kinds = {unit.id: unit_kind(unit) for unit in result.requirements}
        kinds = {**KINDS, "checklist": "Пункт чек-листа"}
        kind_counts = Counter(unit_kinds.values())
        product_count = sum(kind_counts[kind] for kind in REQUIREMENT_KINDS)
        clause_sources = {}
        for unit in result.requirements:
            clause_sources.setdefault(unit.source.location, unit.id)

        priority = {level: i for i, level in enumerate(LEVELS)}
        cards: dict[str, FindingCard] = {}
        counters: Counter[str] = Counter()
        local: dict[str, list[FindingCard]] = defaultdict(list)
        linked: dict[str, list[FindingCard]] = defaultdict(list)
        whole = []
        for finding in result.review.findings:
            is_local = "specification" not in finding.scopes and len(finding.requirement_ids) == 1
            prefix = "D" if is_local else "S"
            counters[prefix] += 1
            evidence: dict[SourceLocation, tuple[Citation, list[str]]] = {}
            for ref in finding.evidence:
                _, owners = evidence.setdefault(ref.location, (ref, []))
                if ref.requirement_id is not None and ref.requirement_id not in owners:
                    owners.append(ref.requirement_id)
            decision = decisions.get(finding.id)
            card = FindingCard(finding, f"{prefix}-{counters[prefix]:02}", decision.level if decision else None,
                               finding_consequence(finding, decision), evidence)
            cards[finding.id] = card
            for owner in finding.requirement_ids:
                linked[owner].append(card)
            if is_local:
                local[finding.requirement_ids[0]].append(card)
            else:
                whole.append(card)

        ordered = sorted(cards.values(), key=lambda card: (not card.blocking, priority.get(card.level, 4), card.label))
        ranks = {card.finding.id: position for position, card in enumerate(ordered)}
        questions = sorted(result.questions or (), key=lambda question: min(ranks[fid] for fid in question.finding_ids))

        def unit_status(unit_id: str) -> str:
            outcomes = {item.outcome for item in evaluations[unit_id]}
            if linked[unit_id]:
                return OUTCOMES["fail"]
            for outcome in ("not_assessable", "not_checked", "pass", "not_applicable"):
                if outcome in outcomes:
                    return OUTCOMES[outcome]
            return OUTCOMES["not_checked"]

        limitations = []
        if not result.requirements:
            limitations.append("Единицы для индивидуальной проверки не извлечены.")
        if unfinished:
            limitations.append(f"Незавершённых оценок (элемент × критерий) — {unfinished}.")
        if result.coverage is None:
            limitations.append("Покрытие исходных блоков не зафиксировано.")
        if any(block.page is not None for block in result.document.blocks):
            limitations.append("Проверка PDF опирается на извлечённый текст страниц.")
        if result.review.rejected:
            limitations.append("У отклонённых замечаний есть отдельные основания в общей проверке.")
        coverage = result.coverage
        coverage_counts = Counter(item.kind for item in coverage or ())
        compliance = compliance_rows(result)
        return self._template.render(
            run=result, rendered_at=rendered_at, criteria=CRITERIA, outcomes=OUTCOMES, levels=LEVELS,
            decisions=decisions, location=location, evaluations=evaluations, counts=counts,
            anchors=anchors, unit_subject=unit_subject, unit_status=unit_status, kinds=kinds, unit_kinds=unit_kinds,
            mixed_kinds=len(kind_counts) > 1, kind_counts=kind_counts, product_count=product_count, unfinished=unfinished,
            outcome_counts=outcome_counts, coverage=coverage, coverage_counts=coverage_counts,
            coverage_kinds=COVERAGE_KINDS, rejected_statuses=REJECTED_STATUSES, questions=questions,
            cards=cards, local=local, linked=linked, whole=whole, ordered=ordered,
            blockers=[card for card in ordered if card.blocking], clause_sources=clause_sources,
            affected_count=sum(bool(items) for items in linked.values()),
            limitations=limitations, diagnostics=self.diagnostics,
            sections={block.id: " → ".join(block.section) for block in result.document.blocks},
            compliance=compliance, compliance_statuses=COMPLIANCE_STATUSES, execution_statuses=EXECUTION_STATUSES,
            compliance_failed=sum(row.status == "fail" for row in compliance),
        )

    def write(self, result: AnalysisRunResult, destination: Path, *, rendered_at: datetime | None = None) -> None:
        write_text_atomic(destination, self.render(result, rendered_at=rendered_at))
