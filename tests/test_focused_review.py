import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from requirements_validator.aggregation import FindingsAggregator, Verdict, Verdicts
from requirements_validator.extractor import Extraction
from requirements_validator.models import QualityCriterion
from requirements_validator.parsers import MarkdownParser
from requirements_validator.pipeline import AnalysisPipeline
from requirements_validator.ports import StructuredResponseError
from requirements_validator.report import MarkdownReportGenerator
from requirements_validator.reviewers import (
    CrossRequirementReviewer, FocusedReview, RelationPlan, RelationReviews, ScenarioConflict, SpecificationReview,
)


def test_competing_readings_retain_the_assumption_and_are_classified_as_ambiguity():
    event = ScenarioConflict.model_validate({
        "subject": "Completion point", "scenario": "Authorization precedes settlement.",
        "relationship": "competing_interpretations",
        "first": {"expected_outcome": "Start the clock at authorization.",
                  "evidence": {"block_id": "a", "quote": "completion", "requirement_id": None}},
        "second": {"expected_outcome": "Start the clock at settlement.",
                   "evidence": {"block_id": "b", "quote": "settled", "requirement_id": None}},
        "recommendation": "Name the triggering stage.",
    }).as_finding()
    assert event.criterion is QualityCriterion.AMBIGUITY
    assert event.scenario.relationship == "competing_interpretations"


@pytest.fixture
def card_spec():
    source = Path(__file__).resolve().parents[1] / "mdfile" / "requirements.md"
    return MarkdownParser().parse(source)


def side(document, quote, expected_outcome):
    block, = [block for block in document.blocks if quote in block.text]
    return {
        "expected_outcome": expected_outcome,
        "evidence": {"block_id": block.id, "quote": quote, "requirement_id": None},
    }


@pytest.mark.parametrize(("prompt_family", "subject", "scenario", "first", "second"), [
    (
        ("state trace", "configuration change"), "Повторный push",
        "Карта уже получила push и достигла повышенного порога в том же месяце.",
        ("На карту приходится максимум один такой push за календарный месяц.", "подавить второй push"),
        ("следующее достижение вызывает push независимо от истории предыдущей настройки", "отправить второй push"),
    ),
    (
        ("time attribution", "different reporting periods"), "Месяц покупки",
        "Покупку от 31 января провели 1 февраля по Алматы.",
        ("Месяц определяется по дате совершения покупки в Алматы.", "увеличить январский счётчик"),
        ("Февральский счётчик вырос на 4 000 ₸", "увеличить февральский счётчик"),
    ),
    (
        ("same operation and invalid input", "4xx response"), "Статус неверной суммы",
        "Клиент передал недопустимую сумму при сохранении порога.",
        ("Ответ в случае, если параметры запроса заполнены неверно.", "HTTP 400"),
        ('{"result":"ERROR","message":"Проверьте сумму"}', "HTTP 200 с ERROR"),
    ),
    (
        ("executable test", "specific exception"), "Статус чужой карты",
        "Пользователь с действующей сессией передал идентификатор чужой карты.",
        ("Ответ в случае, если на указанный ресурс доступ запрещен.", "HTTP 403 по общему правилу"),
        ("Для чужой карты возвращается 404, как и для отсутствующей карты.", "HTTP 404 по особому правилу"),
    ),
], ids=["second-push", "purchase-period", "invalid-amount", "foreign-card"])
def test_focused_scenario_reaches_report(card_spec, llm, prompt_family, subject, scenario, first, second):
    matching_calls = []

    def respond(instruction, payload, schema):
        if schema is Extraction:
            # Acceptance cases and API examples must work without extracted IDs.
            data = json.loads(payload)
            return Extraction.model_validate({"groups": [], "excluded": [
                {"block_id": block["id"], "kind": "supporting_context", "reason": "Проверяется целиком."}
                for block in data["blocks"]
            ]})
        if schema is SpecificationReview:
            return SpecificationReview(findings=())
        if schema is RelationPlan:
            return RelationPlan(links=())
        if schema is FocusedReview:
            assert json.loads(payload)["document"] == card_spec.model_dump(mode="json")
            if not all(selector in instruction for selector in prompt_family):
                return FocusedReview(outcome="not_assessable", rationale="Другой сценарий.", conflicts=())
            matching_calls.append(instruction)
            return FocusedReview.model_validate({
                "outcome": "fail", "rationale": "Описаны разные результаты.",
                "conflicts": [{
                    "subject": subject, "scenario": scenario,
                    "first": side(card_spec, *first), "second": side(card_spec, *second),
                    "recommendation": "Уточнить единый результат этого сценария.",
                }],
            })
        assert schema is Verdicts
        candidate, = json.loads(payload)["candidates"]
        assert {e["quote"] for e in candidate["finding"]["evidence"]} == {first[0], second[0]}
        return Verdicts(verdicts=(Verdict(candidate=0, supported=True, reason="Подтверждено.",
                                          impact_class="blocks_behavior", requires_clarification=True),))

    llm.generate.side_effect = respond
    result = AnalysisPipeline(llm, agent_version="test").analyze(card_spec)
    finding, = result.review.findings
    assert len(matching_calls) == 1
    assert finding.criterion is QualityCriterion.CONSISTENCY
    assert finding.scopes == ("specification",)
    assert scenario in finding.explanation
    assert finding.scenario is not None
    assert (finding.scenario.evidence_a, finding.scenario.evidence_b) == (0, 1)
    assert {finding.scenario.expected_a, finding.scenario.expected_b} == {first[1], second[1]}
    rendered = MarkdownReportGenerator().render(result)
    assert subject in rendered and first[0] in rendered and second[0] in rendered


@pytest.mark.parametrize("claimed_curl_method", ["get", "POST"])
def test_matching_get_and_curl_cannot_produce_focused_method_conflict(card_spec, llm, claimed_curl_method):
    method_calls = []

    def respond(instruction, payload, schema):
        if schema is Extraction:
            data = json.loads(payload)
            return Extraction.model_validate({"groups": [], "excluded": [
                {"block_id": block["id"], "kind": "supporting_context", "reason": "Проверяется целиком."}
                for block in data["blocks"]
            ]})
        if schema is SpecificationReview:
            assert "Created does not identify the request method" in instruction
            return SpecificationReview(findings=())
        if schema is RelationPlan:
            return RelationPlan(links=())
        assert schema is FocusedReview  # No candidate should reach verification.
        if "actual HTTP verb alone" not in instruction:
            return FocusedReview(outcome="not_assessable", rationale="Другой сценарий.", conflicts=())
        method_calls.append(instruction)
        assert "curl -G or --get with data" in instruction
        if len(method_calls) == 2:
            assert "different expected outcomes" in instruction
            return FocusedReview(outcome="pass", rationale="Оба источника указывают GET.", conflicts=())
        try:
            return FocusedReview.model_validate({
                "outcome": "fail", "rationale": "Предполагаемое расхождение.",
                "conflicts": [{
                    "subject": "Расхождение HTTP-методов", "scenario": "Клиент сохраняет порог.",
                    "first": side(card_spec, "| **REST type** | GET |", "GET"),
                    "second": side(card_spec, 'curl -G "$BASE/alerts/saveLimit"', claimed_curl_method),
                    "recommendation": "Уточнить HTTP-метод.",
                }],
            })
        except ValidationError as exc:
            assert "different expected outcomes" in str(exc)
            raise StructuredResponseError("Invalid structured response for FocusedReview") from exc

    llm.generate.side_effect = respond
    result = AnalysisPipeline(llm, agent_version="test").analyze(card_spec)
    assert len(method_calls) == 2
    assert not result.review.findings


def test_relation_ambiguity_row_becomes_grounded_finding(
    document, requirements, llm, assessments,
):
    first, _, last = requirements

    def compare(_instruction, payload, schema):
        assert schema is RelationReviews
        comparison, = json.loads(payload)["comparisons"]
        assert comparison["link"] == 0
        assert comparison["block_ids"] == [block.id for block in document.blocks]
        return RelationReviews.model_validate({"rows": [{
            "link": 0,
            "case": "Остаток требуется показать и скрыть в одном состоянии.",
            "first": {"expected_outcome": "показать остаток", "evidence": {
                "block_id": "b1", "quote": first.source.quote, "requirement_id": first.id,
            }},
            "second": {"expected_outcome": "скрыть остаток", "evidence": {
                "block_id": "b1", "quote": last.source.quote, "requirement_id": last.id,
            }},
            "outcome": "ambiguity",
            "subject": "Видимость остатка",
            "consequence": "Нельзя выбрать состояние интерфейса.",
            "correction": "Задать приоритет правил видимости.",
        }]})

    llm.generate.side_effect = compare
    cross = CrossRequirementReviewer(llm).review_relations(document, requirements)
    proposal, = cross.findings
    assert proposal.criterion is QualityCriterion.AMBIGUITY
    assert proposal.scenario.relationship == "competing_interpretations"

    reviews = {requirement.id: assessments() for requirement in requirements}
    llm.generate.side_effect = None
    llm.generate.return_value = Verdicts(verdicts=(Verdict(
        candidate=0, supported=True, reason="Нельзя однозначно выбрать состояние интерфейса.",
        impact_class="blocks_behavior", requires_clarification=True,
    ),))
    result = FindingsAggregator(llm).aggregate(document, requirements, reviews, cross)
    finding, = result.findings
    assert finding.requirement_ids == tuple(sorted((first.id, last.id)))
    assert finding.scenario is not None
    assert {finding.scenario.expected_a, finding.scenario.expected_b} == {"показать остаток", "скрыть остаток"}
    linked = [row for row in result.evaluations if row.finding_ids == (finding.id,)]
    assert {(row.requirement_id, row.criterion) for row in linked} == {
        (first.id, QualityCriterion.AMBIGUITY), (last.id, QualityCriterion.AMBIGUITY),
    }
