from unittest.mock import Mock

import pytest

from requirements_validator.extractor import Extraction, RequirementsExtractor
from requirements_validator.compliance import DocumentReview
from requirements_validator.executable_model import ExecutableModel
from requirements_validator.models import DOCUMENT_CRITERIA, REQUIREMENT_CRITERIA, ParsedDocument, SourceBlock
from requirements_validator.ports import StructuredLLM
from requirements_validator.reviewers import (
    FOCUSED_CHECKS, Assessment, FocusedReview, IndividualReview, IndividualReviews, LifecycleReview, RelationPlan, RelationReviews,
    RelationRow, TargetReview,
)


@pytest.fixture
def document():
    return ParsedDocument(id="sample", title="Тест", blocks=(
        SourceBlock(id="b1", page=2, section=("Выдача",),
                    text="Р-1: Показать остаток срочно и сохранить чек.\nР-2: Скрыть остаток."),
        SourceBlock(id="b2", section=("Глоссарий",), text="Клиент — владелец счёта."),
    ))


@pytest.fixture
def extraction():
    def anchor(quote):
        return {"block_id": "b1", "quote": quote}

    return Extraction.model_validate({"groups": [
        {"source": anchor("Р-1: Показать остаток срочно и сохранить чек."),
         "original_id": anchor("Р-1"), "atoms": [
             {"statement": "Показать остаток срочно.", "source": anchor("Показать остаток срочно")},
             {"statement": "Сохранить чек.", "source": anchor("сохранить чек")},
         ]},
        {"source": anchor("Р-2: Скрыть остаток."), "original_id": anchor("Р-2"),
         "atoms": [{"statement": "Скрыть остаток.", "source": anchor("Скрыть остаток")}]},
    ], "excluded": [{
        "block_id": "b2", "kind": "supporting_context", "reason": "Определение роли без самостоятельного требования.",
    }]})


@pytest.fixture
def llm():
    return Mock(spec=StructuredLLM, model="mock")


@pytest.fixture
def requirements(document, extraction, llm):
    llm.generate.return_value = extraction
    return RequirementsExtractor(llm).extract(document)


def matrix(*findings):
    rows = []
    for criterion in REQUIREMENT_CRITERIA:
        issues = tuple(f for f in findings if f.criterion == criterion)
        rows.append(Assessment(criterion=criterion, outcome="fail" if issues else "pass",
                               rationale="Оценка исходной формулировки.", findings=issues))
    return IndividualReview(assessments=tuple(rows))


@pytest.fixture
def assessments():
    return matrix


@pytest.fixture
def batch_assessments():
    def review(target_ids=("R-001", "R-002", "R-003")):
        return IndividualReviews(reviews=tuple(
            TargetReview(target_id=target_id, assessments=matrix().assessments) for target_id in target_ids
        ))
    return review


@pytest.fixture
def skipped_comparisons():
    result = FocusedReview(outcome="not_assessable", rationale="Сценарий отсутствует.", conflicts=())
    lifecycle = LifecycleReview(rationale=result.rationale, outcome="not_applicable", checks=())
    return (result,) * len(FOCUSED_CHECKS) + (lifecycle,)


@pytest.fixture
def skipped_relations():
    return RelationReviews(rows=(RelationRow(
        link=0, case="Положения не связаны.", first=None, second=None, outcome="unrelated",
        subject=None, consequence=None, correction=None,
    ),))


@pytest.fixture
def clean_document_review():
    return DocumentReview(assessments=tuple(
        Assessment(criterion=criterion, outcome="pass", rationale="Свойство выполняется.", findings=())
        for criterion in DOCUMENT_CRITERIA
    ))


@pytest.fixture
def empty_executable_model():
    return ExecutableModel(rationale="Исполняемых правил нет.", events=(), state=(), updates=(),
                           obligations=(), invariants=())


@pytest.fixture
def empty_relation_plan():
    return RelationPlan(links=())
