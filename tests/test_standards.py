import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from requirements_validator.aggregation import Verdict, Verdicts
from requirements_validator.compliance import DocumentReview, document_evaluations
from requirements_validator.document_facts import measure
from requirements_validator.executable_model import ExecutableModel
from requirements_validator.extractor import Extraction
from requirements_validator.models import (
    DOCUMENT_CRITERIA, DocumentEvaluation, EvidenceReference, Finding, QualityCriterion as C, ReviewResult,
)
from requirements_validator.parsers import MarkdownParser
from requirements_validator.pipeline import AnalysisPipeline
from requirements_validator.report import MarkdownReportGenerator
from requirements_validator.report_export import to_report
from requirements_validator.reviewers import (
    Assessment, DependencyReview, FocusedReview, IndividualReviews, LifecycleReview, RelationReviews,
    SpecificationReview,
)
from requirements_validator.standards import PROPERTIES, compliance_rows

SPEC = """# Спецификация

## Требования

**Ф-1.** Система сохраняет порог клиента. Приоритет: высокий.

**Ф-2.** Система сохраняет порог клиента.

## Тестирование

заполняются тестировщиками

## Пустой раздел

## Согласование

| Подразделение | ФИО |
| :--- | :--- |
| | |
"""


def metrics_by_name(metrics):
    return {metric.name: metric for metric in metrics}


def test_measured_facts_point_to_source_blocks(tmp_path):
    source = tmp_path / "spec.md"
    source.write_text(SPEC, encoding="utf-8")
    document = MarkdownParser().parse(source)
    metrics = metrics_by_name(measure(document, ()))
    assert metrics["Маркеры приоритета или стабильности"].block_ids == ("L5-L5",)
    assert metrics["Дословно повторяющиеся предложения"].value == "1"
    assert metrics["Дословно повторяющиеся предложения"].block_ids == ("L5-L5", "L7-L7")
    assert metrics["Блоки с незаполненными полями шаблона"].block_ids == ("L11-L11", "L17-L19")
    assert metrics["Заголовки разделов без содержимого"].block_ids == ("L13-L13",)
    assert metrics["Глоссарий или раздел определений"].value == "отсутствует"


def test_traceability_counts_units_without_identifier(document, requirements):
    unnamed = requirements[2].model_copy(update={"original_id": None})
    metrics = metrics_by_name(measure(document, (*requirements[:2], unnamed)))
    traced = metrics["Элементы спецификации с собственным идентификатором"]
    assert (traced.value, traced.block_ids) == ("2 из 3", ("b1",))
    assert metrics["Глоссарий или раздел определений"].value == "есть"


def clean(criterion, outcome="pass", findings=()):
    return Assessment(criterion=criterion, outcome=outcome, rationale="Оценка документа.", findings=findings)


def test_document_review_requires_each_criterion_once():
    complete = DocumentReview(assessments=tuple(clean(c) for c in DOCUMENT_CRITERIA))
    with pytest.raises(ValidationError, match="exactly once"):
        DocumentReview(assessments=complete.assessments[:-1])
    with pytest.raises(ValidationError, match="exactly once"):
        DocumentReview(assessments=(*complete.assessments[:-1], clean(C.ATOMICITY)))


def test_document_evaluations_follow_confirmed_findings(requirements):
    proposal = {"criterion": "ranking", "subject": "Приоритеты", "explanation": "Нет приоритетов.",
                "recommendation": "Указать приоритет.",
                "evidence": [{"block_id": "b1", "quote": "Р-2", "requirement_id": None}]}
    review = DocumentReview.model_validate({"assessments": [
        {"criterion": c, "outcome": "fail" if c in (C.RANKING, C.MODIFIABILITY) else "pass",
         "rationale": "Оценка документа.",
         "findings": [{**proposal, "criterion": c}] if c in (C.RANKING, C.MODIFIABILITY) else []}
        for c in DOCUMENT_CRITERIA
    ]})
    req = requirements[0]
    confirmed = Finding(
        id="F-rank", criterion=C.RANKING, subject="Приоритеты", explanation="Нет приоритетов.",
        recommendation="Указать приоритет.", requirement_ids=(req.id,), scopes=("specification",),
        evidence=(EvidenceReference(**req.source.model_dump(), requirement_id=req.id),),
    )
    conflict = confirmed.model_copy(update={"id": "F-conflict", "criterion": C.CONSISTENCY})
    result = ReviewResult(evaluations=(), findings=(confirmed, conflict), rejected=())
    evaluations = {e.criterion: e for e in document_evaluations(review, result)}
    assert (evaluations[C.RANKING].outcome, evaluations[C.RANKING].finding_ids) == ("fail", ("F-rank",))
    # A conflict confirmed by another whole-specification pass fails the set property too.
    assert evaluations[C.CONSISTENCY].finding_ids == ("F-conflict",)
    # A rejected proposal cannot leave the property failed.
    assert evaluations[C.MODIFIABILITY].outcome == "not_assessable"
    assert evaluations[C.TRACEABILITY].outcome == "pass"


def test_review_result_requires_complete_document_evaluations():
    rows = tuple(DocumentEvaluation(criterion=c, outcome="pass", rationale="Да.", finding_ids=())
                 for c in DOCUMENT_CRITERIA)
    ReviewResult(evaluations=(), findings=(), rejected=(), document_evaluations=rows).verify_evaluations(set(), {})
    with pytest.raises(ValueError, match="exactly once"):
        ReviewResult(evaluations=(), findings=(), rejected=(),
                     document_evaluations=rows[1:]).verify_evaluations(set(), {})


def test_every_standard_clause_maps_to_one_row():
    clauses = [(p.standard, p.clause, p.name) for p in PROPERTIES]
    assert len(clauses) == len(set(clauses)) == 22
    assert {p.criterion for p in PROPERTIES if p.level == "document"} == set(DOCUMENT_CRITERIA)


def test_document_finding_reaches_compliance_matrix(
    document, extraction, batch_assessments, llm, skipped_comparisons, skipped_relations, empty_executable_model,
):
    calls = []

    def respond(_instruction, payload, schema):
        calls.append(schema)
        data = json.loads(payload)
        if schema is Extraction:
            return extraction
        if schema is IndividualReviews:
            return batch_assessments([target["id"] for target in data["targets"]])
        if schema is SpecificationReview:
            return SpecificationReview(findings=())
        if schema is DependencyReview:
            return DependencyReview(dependency_gaps=())
        if schema is FocusedReview:
            return skipped_comparisons[0]
        if schema is LifecycleReview:
            return skipped_comparisons[-1]
        if schema is RelationReviews:
            return skipped_relations
        if schema is DocumentReview:
            names = {metric["name"] for metric in data["measured_facts"]}
            assert "Маркеры приоритета или стабильности" in names
            ranking = {"criterion": "ranking", "subject": "Нет приоритетов", "explanation": "У Р-1 и Р-2 нет приоритета.",
                       "recommendation": "Указать важность и стабильность каждого требования.",
                       "evidence": [{"block_id": "b1", "quote": "Р-2: Скрыть остаток.", "requirement_id": None}]}
            return DocumentReview.model_validate({"assessments": [
                {"criterion": c, "outcome": "fail" if c == C.RANKING else "pass", "rationale": "Оценка документа.",
                 "findings": [ranking] if c == C.RANKING else []}
                for c in DOCUMENT_CRITERIA
            ]})
        if schema is ExecutableModel:
            return empty_executable_model
        assert schema is Verdicts
        return Verdicts(verdicts=(Verdict(candidate=0, supported=True, reason="Приоритеты не указаны.",
                                          impact_class="localized", requires_clarification=False),))

    llm.generate.side_effect = respond
    result = AnalysisPipeline(llm, agent_version="test").analyze(document)
    assert DocumentReview in calls
    finding, = result.review.findings
    assert finding.criterion is C.RANKING and finding.scopes == ("specification",)
    assert {m.criterion for m in result.metrics} >= {C.RANKING, C.TRACEABILITY, C.CONFORMANCE}
    ranking = next(row for row in compliance_rows(result) if row.property.clause == "4.3.5")
    assert (ranking.status, ranking.finding_ids) == ("fail", (finding.id,))
    unambiguous = next(row for row in compliance_rows(result)
                       if row.property.clause == "5.2.5" and row.property.name == "Unambiguous")
    assert unambiguous.status == "pass" and "Требований: 3" in unambiguous.basis

    rendered = MarkdownReportGenerator().render(result)
    section = rendered.split("## Соответствие стандартам", 1)[1].split("## Покрытие документа", 1)[0]
    assert "| IEEE Std 830-1998, п. 4.3.5 | Ранжированность (Ranked for importance and/or stability) | Не соответствует |" in section
    assert "ISO/IEC/IEEE 29148:2018, п. 5.2.6" in section and "Измеримые показатели документа" in section
    assert "Свойства стандартов с нарушениями | 1 из 22" in rendered

    exported = to_report(result, Path("spec.md"))
    row = next(item for item in exported.compliance if item.clause == "4.3.5")
    assert (row.status, row.findings) == ("Не соответствует", ["ДН-01"])


def test_language_lint_quotes_the_sentence_with_each_weak_term(tmp_path):
    from requirements_validator.extractor import RequirementsExtractor
    from requirements_validator.language_lint import lint
    from requirements_validator.source import SourceIndex
    from unittest.mock import Mock

    source = tmp_path / "spec.md"
    source.write_text("# Спец\n\nТ-1. Система быстро сохраняет порог. Ответ показывается быстро и по возможности "
                      "удобно.\nТ-2. Порог от 1 000 до 5 000 ₸.\n", encoding="utf-8")
    document = MarkdownParser().parse(source)
    block = document.blocks[1]
    llm = Mock(model="mock")
    llm.generate.return_value = Extraction.model_validate({"groups": [
        {"source": {"block_id": block.id, "quote": line}, "original_id": {"block_id": block.id, "quote": label},
         "atoms": [{"statement": line}]}
        for label, line in (("Т-1", block.text.splitlines()[0]), ("Т-2", block.text.splitlines()[1]))
    ], "excluded": [{"block_id": document.blocks[0].id, "kind": "metadata", "reason": "Заголовок."}]})
    requirements = RequirementsExtractor(llm).extract(document)
    findings = {f.subject: f for f in lint(document, requirements)}
    assert set(findings) == {"Неопределённая мера в R-001", "Лазейка в R-001", "Субъективная формулировка в R-001"}
    vague = findings["Неопределённая мера в R-001"]
    assert vague.criterion is C.AMBIGUITY and "«быстро»" in vague.explanation and "5.2.7" in vague.explanation
    index = SourceIndex(document)
    for evidence in vague.evidence:
        citation = index.resolve(evidence, within=requirements[0].source.location)
        assert "быстро" in citation.quote and evidence.requirement_id == "R-001"
    assert len(vague.evidence) == 2  # Both sentences, each grounded separately.
