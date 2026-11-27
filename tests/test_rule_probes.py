import json
from pathlib import Path

import pytest

from requirements_validator.aggregation import Verdict, Verdicts
from requirements_validator.compliance import DocumentReview
from requirements_validator.executable_model import ExecutableModel
from requirements_validator.extractor import Extraction
from requirements_validator.models import ParsedDocument, QualityCriterion, SourceBlock
from requirements_validator.parsers import MarkdownParser
from requirements_validator.pipeline import AnalysisPipeline
from requirements_validator.report import MarkdownReportGenerator
from requirements_validator.reviewers import (
    DependencyReview, FocusedReview, LifecycleReview, RelationPlan, SpecificationReview,
)
from requirements_validator.rule_probes import (
    CapVerdicts, ReadVerdicts, RuleProbes, inventory_operations, operation_kind,
)

CAP = "На карту приходится максимум один такой push за календарный месяц."
OVERRIDE = "Если она ниже нового порога, следующее достижение вызывает push независимо от истории предыдущей настройки."
READ = "Приложение читает и сохраняет настройки через дочерний документ «Уведомление о расходах по карте — REST API»."


@pytest.fixture
def card_spec():
    return MarkdownParser().parse(Path(__file__).resolve().parents[1] / "mdfile" / "requirements.md")


@pytest.mark.parametrize(("endpoint", "kind"), [
    ("/alerts/saveLimit", "write"), ("/alerts/getLimit", "read"), ("/v1/cards/{id}/update_threshold", "write"),
    ("/alerts/limits/list", "read"), ("/alerts/limit", "unknown"),
])
def test_operation_kind_uses_the_verb_in_the_endpoint_name(endpoint, kind):
    assert operation_kind(endpoint) == kind


def test_inventory_quotes_the_table_cell_that_declares_the_endpoint(card_spec):
    operation, = inventory_operations(card_spec, ())
    assert operation.endpoint == "/alerts/saveLimit"
    assert operation.kind == "write"
    block = next(b for b in card_spec.blocks if b.id == operation.passage.block_id)
    assert operation.passage.quote in block.text and "|" not in operation.passage.quote


def test_probes_skip_the_model_without_candidate_passages(llm):
    document = ParsedDocument(id="d", title="t", blocks=(
        SourceBlock(id="b1", text="Система сохраняет заказ. Заказ получает номер."),
    ))
    assert RuleProbes(llm).review(document, ()) == ()
    llm.generate.assert_not_called()


def test_read_probe_is_skipped_when_a_read_operation_is_documented(llm):
    document = ParsedDocument(id="d", title="t", blocks=(
        SourceBlock(id="b1", text="Приложение читает настройки через API."),
        SourceBlock(id="b2", text="| URL | `/alerts/saveLimit` |\n| URL чтения | `/alerts/getLimit` |"),
    ))
    assert RuleProbes(llm).review(document, ()) == ()
    llm.generate.assert_not_called()


def test_cap_override_and_missing_read_reach_the_report(card_spec, llm, clean_document_review,
                                                        empty_executable_model):
    seen = {}

    def respond(instruction, payload, schema):
        if schema is Extraction:
            return Extraction.model_validate({"groups": [], "excluded": [
                {"block_id": block["id"], "kind": "supporting_context", "reason": "Проверяется целиком."}
                for block in json.loads(payload)["blocks"]
            ]})
        if schema is DependencyReview:
            return DependencyReview(dependency_gaps=())
        if schema is SpecificationReview:
            return SpecificationReview(findings=())
        if schema is RelationPlan:
            return RelationPlan(links=())
        if schema is DocumentReview:
            return clean_document_review
        if schema is ExecutableModel:
            return empty_executable_model
        if schema is LifecycleReview:
            return LifecycleReview(rationale="Нет этапов.", outcome="not_applicable", checks=())
        if schema is FocusedReview:
            return FocusedReview(outcome="pass", rationale="Нет расхождений.", conflicts=())
        if schema is CapVerdicts:
            pairs = json.loads(payload)["pairs"]
            seen["pairs"] = pairs
            pair = next(p["pair"] for p in pairs if p["cap"] == CAP and p["override"] == OVERRIDE)
            return CapVerdicts.model_validate({"rationale": "Сценарий со сменой порога.", "verdicts": [{
                "pair": pair, "outcome": "conflict",
                "scenario": "Порог 50 000 ₸ достигнут, push отправлен; клиент поднял порог до 60 000 ₸ и достиг его в том же месяце.",
                "cap_outcome": "второй push за месяц запрещён", "override_outcome": "второй push обязателен",
                "subject": "Лимит одного push в месяц противоречит push после смены порога",
                "consequence": "Тест второго достижения порога не имеет ожидаемого результата.",
                "recommendation": "Указать, какое правило имеет приоритет.",
            }]})
        if schema is ReadVerdicts:
            data = json.loads(payload)
            seen["operations"] = data["operations"]
            demand = next(d["index"] for d in data["demands"] if d["quote"] == READ)
            return ReadVerdicts.model_validate({"rationale": "Есть только сохранение.", "verdicts": [{
                "demand": demand, "served_by": None, "gap": True, "missing_values": "текущий порог и переключатель",
                "subject": "Нет операции чтения настроек уведомления",
                "consequence": "Экран нельзя заполнить сохранёнными значениями.",
                "recommendation": "Добавить в часть B операцию чтения настроек.",
            }]})
        assert schema is Verdicts
        candidates = json.loads(payload)["candidates"]
        return Verdicts(verdicts=tuple(Verdict(candidate=c["candidate"], supported=True, reason="Подтверждено.",
                                               impact_class="blocks_behavior", requires_clarification=True)
                                       for c in candidates))

    llm.generate.side_effect = respond
    result = AnalysisPipeline(llm, agent_version="test").analyze(card_spec)

    assert seen["operations"][0]["endpoint"] == "/alerts/saveLimit"
    by_criterion = {f.criterion: f for f in result.review.findings}
    conflict = by_criterion[QualityCriterion.CONSISTENCY]
    assert {e.quote for e in conflict.evidence} == {CAP, OVERRIDE}
    assert conflict.scenario is not None and conflict.scenario.expected_a == "второй push за месяц запрещён"
    gap = by_criterion[QualityCriterion.COMPLETENESS]
    assert READ in {e.quote for e in gap.evidence}
    assert "/alerts/saveLimit" in gap.explanation
    rendered = MarkdownReportGenerator().render(result)
    assert "Лимит одного push в месяц" in rendered and "Нет операции чтения настроек" in rendered
