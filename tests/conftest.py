from unittest.mock import Mock

import pytest

from requirements_validator.extractor import Extraction, RequirementsExtractor
from requirements_validator.models import ParsedDocument, QualityCriterion, SourceBlock
from requirements_validator.ports import StructuredLLM
from requirements_validator.reviewers import (
    FOCUSED_CHECKS, Assessment, FocusedReview, IndividualReview, RelationPlan, RelationReviews, RelationRow,
)


@pytest.fixture
def document():
    return ParsedDocument(id="sample", title="Тест", blocks=(
        SourceBlock(id="b1", page=2, section=("Выдача",),
                    text="Р-1: Показать остаток быстро и сохранить чек.\nР-2: Скрыть остаток."),
        SourceBlock(id="b2", section=("Глоссарий",), text="Клиент — владелец счёта."),
    ))


@pytest.fixture
def extraction():
    def anchor(quote):
        return {"block_id": "b1", "quote": quote}

    return Extraction.model_validate({"groups": [
        {"source": anchor("Р-1: Показать остаток быстро и сохранить чек."),
         "original_id": anchor("Р-1"), "atoms": [
             {"statement": "Показать остаток быстро.", "source": anchor("Показать остаток быстро")},
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
    for criterion in QualityCriterion:
        issues = tuple(f for f in findings if f.criterion == criterion)
        rows.append(Assessment(criterion=criterion, outcome="fail" if issues else "pass",
                               rationale="Оценка исходной формулировки.", findings=issues))
    return IndividualReview(assessments=tuple(rows))


@pytest.fixture
def assessments():
    return matrix


@pytest.fixture
def skipped_comparisons():
    result = FocusedReview(outcome="not_assessable", rationale="Сценарий отсутствует.", conflicts=())
    return (result,) * len(FOCUSED_CHECKS)


@pytest.fixture
def skipped_relations():
    return RelationReviews(rows=(RelationRow(
        link=0, case="Положения не связаны.", first=None, second=None, outcome="unrelated",
        subject=None, consequence=None, correction=None,
    ),))


@pytest.fixture
def empty_relation_plan():
    return RelationPlan(links=())
