import os
import re
from collections import Counter, defaultdict
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile

from jinja2 import Environment, PackageLoader, StrictUndefined

from .models import (
    AnalysisRunResult, Citation, ClarificationQuestion, CriterionEvaluation,
    QualityCriterion as Criterion,
)
from .source import SourceIndex, verify_evidence_owner
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
OUTCOMES = {"pass": "Пройдено", "fail": "Нарушение", "not_assessable": "Нельзя оценить"}
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
    if result.review.rejected or any(e.outcome == "not_assessable" for e in result.review.evaluations):
        return False, "Подтверждённых дефектов нет, но вывод о готовности ограничен: остались непроверяемые критерии или неподтверждённые замечания."
    return True, "В пределах проанализированного материала подтверждённых препятствий для начала реализации и тестирования не выявлено."


class MarkdownReportGenerator:
    def __init__(self) -> None:
        env = Environment(loader=PackageLoader("requirements_validator"), undefined=StrictUndefined,
                          trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
        env.filters.update(md=inline, quotation=quotation)
        self._template = env.get_template("report.md.j2")

    def render(self, result: AnalysisRunResult) -> str:
        index = SourceIndex(result.document)
        requirements = {requirement.id: requirement for requirement in result.requirements}
        for requirement in result.requirements:
            index.verify_requirement(requirement)
        for finding in result.review.findings:
            for ref in finding.evidence:
                verify_evidence_owner(ref, requirements)

        def location(citation: Citation) -> str:
            index.verify(citation)
            span = citation.location
            block = index.blocks[span.block_id]
            prefix = f"стр. {block.page}, " if block.page is not None else ""
            return f"{prefix}{inline(block.id)}, символы [{span.start}, {span.end})"

        evaluations: dict[str, list[CriterionEvaluation]] = defaultdict(list)
        for evaluation in result.review.evaluations:
            evaluations[evaluation.requirement_id].append(evaluation)
        decisions = {decision.finding_id: decision for decision in result.severities or ()}
        findings = result.review.findings
        counts = Counter(decisions[f.id].level for f in findings if f.id in decisions)
        outcome_counts = Counter(e.outcome for e in result.review.evaluations)
        critical = [f for f in findings if f.id in decisions and decisions[f.id].level in {"critical", "high"}]
        _, readiness = report_readiness(result, has_blocking_findings=bool(critical))
        priority = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        ordered = sorted(findings, key=lambda f: (priority[decisions[f.id].level] if f.id in decisions else 4, f.id))

        finding_by_id = {finding.id: finding for finding in findings}

        def question_priority(question: ClarificationQuestion) -> tuple[int, str]:
            linked = [finding_by_id[fid] for fid in question.finding_ids if fid in finding_by_id]
            ranks = [priority[decisions[f.id].level] for f in linked if f.id in decisions]
            rank = min(ranks, default=4)
            label = ("Высокий" if rank <= 1 else "Средний" if rank == 2 else
                     "Низкий" if rank == 3 else "Не определён")
            return rank, label

        question_rows = []
        for position, question in enumerate(result.questions or ()):
            rank, label = question_priority(question)
            question_rows.append((rank, position, label, question))
        question_rows.sort(key=lambda row: (row[0], row[1]))

        coverage = getattr(result, "coverage", None)
        coverage_rows = []
        if coverage is not None:
            block_order = {block.id: position for position, block in enumerate(result.document.blocks)}
            ordered_coverage = sorted(
                coverage, key=lambda item: (block_order.get(item.block_id, len(block_order)), item.block_id)
            )
            for item in ordered_coverage:
                block = index.blocks[item.block_id]
                section = " → ".join(block.section) or result.document.title
                coverage_rows.append((item, section))

        def normalized(text: str) -> str:
            return " ".join(text.split()).casefold()

        show_impact = {
            finding.id: bool(
                finding.impact
                and (
                    finding.id not in decisions
                    or normalized(finding.impact) != normalized(decisions[finding.id].rationale)
                )
            )
            for finding in findings
        }

        rejected_counts = Counter(getattr(item, "status", "unsupported") for item in result.review.rejected)
        compliance = compliance_rows(result)
        return self._template.render(
            run=result, criteria=CRITERIA, outcomes=OUTCOMES, levels=LEVELS, decisions=decisions,
            location=location, evaluations=evaluations, counts=counts, critical=critical,
            unclassified=len(findings) - sum(counts.values()), readiness=readiness, ordered=ordered,
            whole=[f for f in findings if "specification" in f.scopes],
            kinds=KINDS, coverage_kinds=COVERAGE_KINDS, rejected_statuses=REJECTED_STATUSES,
            outcome_counts=outcome_counts, question_rows=question_rows, coverage=coverage,
            coverage_rows=coverage_rows, rejected_counts=rejected_counts, show_impact=show_impact,
            compliance=compliance, compliance_statuses=COMPLIANCE_STATUSES, execution_statuses=EXECUTION_STATUSES,
            compliance_failed=sum(row.status == "fail" for row in compliance),
        )

    def write(self, result: AnalysisRunResult, destination: Path) -> None:
        rendered = self.render(result)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=destination.parent,
                                    prefix=".requirements-report-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(rendered)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
