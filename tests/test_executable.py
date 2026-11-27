from pathlib import Path

import pytest

from requirements_validator.executable_engine import Engine
from requirements_validator.executable_model import (
    Context, EvaluationError, ExecutableModel, Keyed, TestCase, evaluate, names,
)
from requirements_validator.parsers import MarkdownParser

SPEC = Path(__file__).resolve().parents[1] / "mdfile" / "requirements.md"
RULES = "L37-L41"
MONTH = 'key(event.card, month(event.purchased_at, "Asia/Almaty"))'


def anchor(quote, block=RULES):
    return {"block_id": block, "quote": quote}


@pytest.fixture
def card_spec():
    return MarkdownParser().parse(SPEC)


@pytest.fixture
def card_model():
    """A faithful hand translation of Ф-1…Ф-4 and the refund rule."""
    return ExecutableModel.model_validate({
        "rationale": "Ручной перевод правил Ф-1…Ф-4.",
        "events": [
            {"name": "purchase", "description": "Проведённая покупка", "source": anchor("проведена покупка", "L43-L43"),
             "fields": [{"name": "card", "kind": "text", "source": anchor("Хранить идентификатор карты", "L49-L49")},
                        {"name": "amount", "kind": "number", "source": anchor("сумму покупки в тиынах", "L49-L49")},
                        {"name": "purchased_at", "kind": "timestamp",
                         "source": anchor("Месяц определяется по дате совершения покупки в Алматы")},
                        {"name": "posted_at", "kind": "timestamp", "source": anchor("проведена покупка", "L43-L43")}]},
            {"name": "transfer", "description": "Перевод", "source": anchor("Переводы и снятие наличных исключаются"),
             "fields": [{"name": "card", "kind": "text", "source": None},
                        {"name": "amount", "kind": "number", "source": None}]},
            {"name": "refund", "description": "Возврат", "source": anchor("Возврат уменьшает расходы месяца исходной покупки"),
             "fields": [{"name": "card", "kind": "text", "source": anchor("Хранить идентификатор карты", "L49-L49")},
                        {"name": "amount", "kind": "number", "source": anchor("сумму покупки в тиынах", "L49-L49")},
                        {"name": "original_purchased_at", "kind": "timestamp", "source": None}]},
            {"name": "change_threshold", "description": "Изменение порога",
             "source": anchor("При изменении порога накопленная сумма сохраняется"),
             "fields": [{"name": "card", "kind": "text", "source": anchor("Хранить идентификатор карты", "L49-L49")},
                        {"name": "value", "kind": "number", "source": anchor("порог в тенге", "L49-L49")},
                        {"name": "at", "kind": "timestamp", "source": None}]},
        ],
        "state": [
            {"name": "spent", "keyed": True, "default": "0", "description": "Расходы карты за месяц"},
            {"name": "threshold", "keyed": True, "default": "0", "description": "Порог карты"},
            {"name": "rearmed", "keyed": True, "default": "False", "description": "Порог изменён ниже итога"},
        ],
        "updates": [
            {"id": "F2_month", "on": "purchase", "target": "spent", "key": MONTH, "op": "add",
             "value": "event.amount", "source": anchor("Месяц определяется по дате совершения покупки в Алматы")},
            {"id": "F2_refund", "on": "refund", "target": "spent", "op": "subtract", "value": "event.amount",
             "key": 'key(event.card, month(event.original_purchased_at, "Asia/Almaty"))',
             "source": anchor("Возврат уменьшает расходы месяца исходной покупки")},
            {"id": "F1_threshold", "on": "change_threshold", "target": "threshold", "key": "event.card", "op": "set",
             "value": "event.value",
             "source": anchor("Держатель активной карты KZT задаёт целый порог от 1 000 до 1 000 000 ₸ включительно")},
            {"id": "F4_rearm", "on": "change_threshold", "target": "rearmed", "key": "event.card", "op": "set",
             "value": "True",
             "when": 'spent[key(event.card, month(event.at, "Asia/Almaty"))] < event.value',
             "source": anchor("Если она ниже нового порога, следующее достижение вызывает push "
                              "независимо от истории предыдущей настройки")},
        ],
        "obligations": [
            {"id": "F3_push", "on": "purchase", "output": "push",
             "when": f"prev.spent[{MONTH}] < threshold[event.card] <= spent[{MONTH}] "
                     'and count("push", card=event.card, month=month(event.purchased_at, "Asia/Almaty")) == 0',
             "fields": [{"name": "card", "value": "event.card"},
                        {"name": "month", "value": 'month(event.purchased_at, "Asia/Almaty")'}],
             "source": anchor("Когда расходы переходят от суммы ниже порога к сумме, равной порогу или выше, "
                              "клиент получает push")},
            {"id": "F4_push", "on": "purchase", "output": "push",
             "when": f"rearmed[event.card] and prev.spent[{MONTH}] < threshold[event.card] <= spent[{MONTH}]",
             "fields": [{"name": "card", "value": "event.card"},
                        {"name": "month", "value": 'month(event.purchased_at, "Asia/Almaty")'}],
             "source": anchor("Если она ниже нового порога, следующее достижение вызывает push "
                              "независимо от истории предыдущей настройки")},
        ],
        "invariants": [
            {"id": "F3_cap", "check": 'max_count("push", "card", "month") <= 1',
             "source": anchor("На карту приходится максимум один такой push за календарный месяц")},
        ],
    })


def case(data):
    return TestCase.model_validate({"initial": [], "expectations": [], **data})


def purchase(amount, at, card="c1", posted=None):
    return {"type": "purchase", "fields": [{"name": "card", "value": f'"{card}"'},
                                           {"name": "amount", "value": str(amount)},
                                           {"name": "purchased_at", "value": f'"{at}"'},
                                           {"name": "posted_at", "value": f'"{posted or at}"'}]}


def table(quote):
    return anchor(quote, "L61-L65")


def test_expressions_are_sandboxed():
    state = {"spent": Keyed(0)}
    assert evaluate('month("2026-01-31T23:58", "Asia/Almaty")', Context(state)) == "2026-01"
    assert evaluate('month("2026-01-31T19:30:00+00:00", "Asia/Almaty")', Context(state)) == "2026-02"
    assert evaluate('spent["missing"] + 1', Context(state)) == 1
    for unsafe in ("__import__('os')", "().__class__", "open('x')", "spent.__class__", "lambda: 1"):
        with pytest.raises(EvaluationError):
            evaluate(unsafe, Context(state))
    assert names(f'prev.spent[{MONTH}] < threshold[event.card] and count("push") == 0') >= {
        "prev.spent", "threshold", "event.card", "output:push"}


def test_acceptance_rows_pass_or_expose_the_period_conflict(card_model):
    engine = Engine(card_model)
    assert not engine.invalid
    first = engine.run(case({
        "id": "AC1", "kind": "acceptance", "description": "Порог достигнут ровно",
        "source": table("| Расходы 48 500 ₸, порог 50 000 ₸ | Провести покупку на 1 500 ₸ | Итог 50 000 ₸ и один push |"),
        "initial": [{"variable": "spent", "key": '"c1|2026-03"', "value": "48500"},
                    {"variable": "threshold", "key": '"c1"', "value": "50000"}],
        "events": [purchase(1500, "2026-03-10T12:00")],
        "expectations": [{"check": 'spent["c1|2026-03"] == 50000', "description": "Итог 50 000 ₸"},
                         {"check": 'count("push") == 1', "description": "один push"}],
    }))
    assert first.status == "pass"

    boundary = engine.run(case({
        "id": "AC2", "kind": "acceptance", "description": "Покупка 31 января, проведение 1 февраля",
        "source": table("| Покупка 31 января в 23:58 на 4 000 ₸ | Провести 1 февраля в 00:03 по Алматы | "
                        "Февральский счётчик вырос на 4 000 ₸ |"),
        "events": [purchase(4000, "2026-01-31T23:58", posted="2026-02-01T00:03")],
        "expectations": [{"check": 'spent["c1|2026-02"] == 4000', "description": "Февральский счётчик вырос на 4 000 ₸"}],
    }))
    problem, = boundary.problems
    assert boundary.status == "mismatch" and problem.rule_ids == ("F2_month",)
    assert problem.computed == "spent['c1|2026-02'] = 0"


def test_threshold_change_after_a_push_breaks_the_monthly_cap(card_model):
    change = {"type": "change_threshold", "fields": [{"name": "card", "value": '"c1"'}, {"name": "value", "value": "60000"},
                                                     {"name": "at", "value": '"2026-03-06T09:00"'}]}
    result = Engine(card_model).run(case({
        "id": "P1", "kind": "probe", "description": "Порог повышен после push в том же месяце",
        "source": None, "initial": [{"variable": "threshold", "key": '"c1"', "value": "50000"}],
        "events": [purchase(50000, "2026-03-05T10:00"), change, purchase(10000, "2026-03-07T10:00")],
    }))
    conflict, = result.problems
    assert conflict.kind == "conflict" and conflict.step == 3
    # The push rule and the earlier update that re-armed it are both causes.
    assert conflict.rule_ids[:2] == ("F3_cap", "F4_push") and "F4_rearm" in conflict.rule_ids
    assert [item["_rule"] for item in result.outputs] == ["F3_push", "F4_push"]


def test_refund_needs_an_undocumented_input(card_model):
    result = Engine(card_model).run(case({
        "id": "P2", "kind": "probe", "description": "Возврат покупки", "source": None,
        "events": [{"type": "refund", "fields": [{"name": "card", "value": '"c1"'}, {"name": "amount", "value": "500"}]}],
    }))
    problem, = result.problems
    assert problem.kind == "missing_input" and problem.field == "original_purchased_at"
    assert problem.rule_ids == ("F2_refund",)


def acceptance_cases():
    return [
        {"id": "AC1", "kind": "acceptance", "description": "Порог достигнут ровно",
         "source": table("| Расходы 48 500 ₸, порог 50 000 ₸ | Провести покупку на 1 500 ₸ | Итог 50 000 ₸ и один push |"),
         "initial": [{"variable": "spent", "key": '"c1|2026-03"', "value": "48500"},
                     {"variable": "threshold", "key": '"c1"', "value": "50000"}],
         "events": [purchase(1500, "2026-03-10T12:00")],
         "expectations": [{"check": 'count("push") == 1', "description": "один push"}]},
        {"id": "AC2", "kind": "acceptance", "description": "Покупка 31 января, проведение 1 февраля",
         "source": table("| Покупка 31 января в 23:58 на 4 000 ₸ | Провести 1 февраля в 00:03 по Алматы | "
                         "Февральский счётчик вырос на 4 000 ₸ |"),
         "events": [purchase(4000, "2026-01-31T23:58", posted="2026-02-01T00:03")],
         "expectations": [{"check": 'spent["c1|2026-02"] == 4000',
                           "description": "Февральский счётчик вырос на 4 000 ₸"}]},
    ]


def probe_cases():
    change = {"type": "change_threshold", "fields": [{"name": "card", "value": '"c1"'},
                                                     {"name": "value", "value": "60000"},
                                                     {"name": "at", "value": '"2026-03-06T09:00"'}]}
    undated = {**change, "fields": change["fields"][:2]}
    return [
        {"id": "P1", "kind": "probe", "description": "Порог повышен после push", "source": None,
         "initial": [{"variable": "threshold", "key": '"c1"', "value": "50000"}],
         "events": [purchase(50000, "2026-03-05T10:00"), change, purchase(10000, "2026-03-07T10:00")],
         # A probe's own expectation is ignored: only computed interactions count.
         "expectations": [{"check": "False", "description": "Модель ожидала ошибку"}]},
        {"id": "P2", "kind": "probe", "description": "Возврат покупки", "source": None,
         "events": [{"type": "refund", "fields": [{"name": "card", "value": '"c1"'}, {"name": "amount", "value": "500"}]}]},
        {"id": "P3", "kind": "probe", "description": "Изменение порога без времени", "source": None,
         "events": [undated]},
    ]


def test_reviewer_turns_computed_results_into_grounded_findings(card_spec, card_model, llm):
    from requirements_validator.executable_model import TestCases
    from requirements_validator.executable_review import ExecutableReviewer
    from requirements_validator.source import SourceIndex

    # The undocumented change_threshold "at" field is supplied in P1 and omitted only in P3.
    cases = TestCases.model_validate({"cases": acceptance_cases() + probe_cases()})
    llm.generate.side_effect = [card_model, cases]
    review = ExecutableReviewer(llm).review(card_spec, ())
    rows = {row.case: row.status for row in review.rows}
    assert rows == {"AC1": "pass", "AC2": "mismatch", "P1": "conflict", "P2": "missing_input", "P3": "missing_input"}
    findings = {f.subject: f for f in review.findings.findings}
    period = next(f for s, f in findings.items() if s.startswith("Пример приёмки"))
    assert period.scenario.expected_a.endswith("Февральский счётчик вырос на 4 000 ₸")
    assert "Месяц определяется по дате совершения покупки в Алматы" in {e.quote for e in period.evidence}
    cap = next(f for s, f in findings.items() if s.startswith("Правила требуют"))
    assert [e.quote for e in cap.evidence] == [
        "Если она ниже нового порога, следующее достижение вызывает push независимо от истории предыдущей настройки",
        "На карту приходится максимум один такой push за календарный месяц",
    ]
    assert {"Не задан источник данных: original_purchased_at", "Не задан источник данных: at"} <= set(findings)
    index = SourceIndex(card_spec)
    for finding in review.findings.findings:
        for evidence in finding.evidence:
            index.resolve(evidence)


def test_untrusted_translation_reports_no_mismatches(card_spec, card_model, llm):
    from requirements_validator.executable_model import TestCases
    from requirements_validator.executable_review import ExecutableReviewer

    wrong = acceptance_cases()
    wrong[0]["expectations"] = [{"check": 'count("push") == 2', "description": "два push"}]
    llm.generate.side_effect = [card_model, TestCases.model_validate({"cases": wrong})]
    review = ExecutableReviewer(llm).review(card_spec, ())
    assert [row.status for row in review.rows] == ["mismatch", "mismatch"]
    assert review.findings.findings == ()


def test_rules_with_invented_quotes_are_dropped(card_spec, card_model, llm):
    from requirements_validator.executable_model import TestCases
    from requirements_validator.executable_review import ExecutableReviewer

    from requirements_validator.models import Anchor

    invented = card_model.invariants[0].model_copy(update={"source": Anchor(**anchor("Не более двух уведомлений в день"))})
    model = card_model.model_copy(update={"invariants": (invented,)})
    # The model keeps the invented quotation through both repair rounds.
    llm.generate.side_effect = [model, model, model, TestCases.model_validate({"cases": probe_cases()[:1]})]
    review = ExecutableReviewer(llm).review(card_spec, ())
    assert all(row.status != "conflict" for row in review.rows)
    repair = llm.generate.call_args_list[1].args
    assert "F3_cap: source quote is not an exact passage" in repair[1]


def test_diagnostics_drive_a_repair_of_the_translation(card_spec, card_model, llm):
    from requirements_validator.executable_engine import diagnose
    from requirements_validator.executable_model import TestCases
    from requirements_validator.executable_review import ExecutableReviewer

    assert diagnose(card_model) == []
    broken_cap = card_model.invariants[0].model_copy(update={"check": "spent <= 1"})
    broken = card_model.model_copy(update={"invariants": (broken_cap,)})
    assert diagnose(broken) == ["F3_cap: spent is keyed, so read it as spent[key] in `spent <= 1`"]
    llm.generate.side_effect = [broken, card_model, TestCases.model_validate({"cases": probe_cases()[:1]})]
    review = ExecutableReviewer(llm).review(card_spec, ())
    assert [row.status for row in review.rows] == ["conflict"]
    instruction, payload, _ = llm.generate.call_args_list[1].args
    assert "previous_model failed the checks" in instruction and "spent is keyed" in payload


def test_case_diagnostics_catch_generation_mistakes(card_model):
    from requirements_validator.executable_engine import diagnose_cases

    single_time = card_model.model_copy(update={"events": (
        card_model.events[0].model_copy(update={"fields": card_model.events[0].fields[:3]}), *card_model.events[1:])})
    bad = TestCase.model_validate({
        "id": "AC2", "kind": "acceptance", "description": "Граница месяца", "source": None,
        "events": [purchase(4000, "2026-02-01T00:03")],
        "expectations": [{"check": 'spent[\'key("c1", "2026-02")\'] == 4000', "description": "Февраль"},
                         {"check": 'count("push_notification") == 0', "description": "без push"}],
    })
    row = "| Покупка 31 января в 23:58 на 4 000 ₸ | Провести 1 февраля в 00:03 по Алматы | ... |"
    found = diagnose_cases((bad,), single_time, {"AC2": row})
    assert any("not inside a string" in item for item in found)
    assert any("no rule emits output 'push_notification'" in item for item in found)
    assert any("states 2 different times" in item and "['purchased_at']" in item for item in found)
    assert any("states the time 23:58 but no event input contains it" in item for item in found)
    faithful = bad.model_copy(update={"expectations": (), "events": TestCase.model_validate({
        **bad.model_dump(), "events": [purchase(4000, "2026-01-31T23:58", posted="2026-02-01T00:03")]}).events})
    assert not diagnose_cases((faithful,), card_model, {"AC2": row})


def test_paraphrased_acceptance_row_resolves_to_exact_source_line(card_spec):
    from requirements_validator.executable_review import resolve_row
    from requirements_validator.models import Anchor
    from requirements_validator.source import SourceIndex

    index = SourceIndex(card_spec)
    row = resolve_row(Anchor(block_id="L61-L65", quote="Расходы 19 900 ₸, порог 20 000 ₸  Перевести 500 ₸"), index)
    assert row.quote == "| Расходы 19 900 ₸, порог 20 000 ₸ | Перевести 500 ₸ | Счётчик показывает 19 900 ₸ |"
    index.verify(row)
    assert resolve_row(Anchor(block_id="L61-L65", quote="₸"), index) is None  # Ambiguous across rows.


def test_max_count_must_group_by_output_fields(card_model):
    from requirements_validator.executable_engine import diagnose

    cap = card_model.invariants[0].model_copy(update={"check": 'max_count("push", "card", month("2026-01-01")) <= 1'})
    found = diagnose(card_model.model_copy(update={"invariants": (cap,)}))
    assert found and "max_count groups by output field names ['card', 'month']" in found[0]
