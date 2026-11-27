"""Deterministic execution of test cases against an executable model."""

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .executable_model import (
    Context, EvaluationError, ExecutableModel, Expectation, Keyed, MissingInput, TestCase, evaluate, literal,
    names, parse,
)

LOG = logging.getLogger(__name__)
Status = Literal["pass", "conflict", "mismatch", "missing_input", "error"]
PRIORITY: tuple[Status, ...] = ("conflict", "mismatch", "missing_input", "error")


@dataclass(frozen=True)
class Problem:
    kind: Literal["conflict", "mismatch", "missing_input", "error"]
    message: str
    rule_ids: tuple[str, ...] = ()
    expectation: Expectation | None = None
    computed: str = ""
    step: int | None = None
    field: str | None = None


@dataclass
class CaseResult:
    case: TestCase
    problems: list[Problem] = field(default_factory=list)
    fired: list[str] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> Status:
        kinds = {problem.kind for problem in self.problems}
        return next((kind for kind in PRIORITY if kind in kinds), "pass")


def _coerce(value: Any, kind: str) -> Any:
    if kind == "number" and isinstance(value, str):
        cleaned = value.replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            number = float(cleaned)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    if kind == "flag" and isinstance(value, (int, str)) and not isinstance(value, bool):
        return str(value).strip().casefold() in {"1", "true", "yes", "да"}
    return value


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {name: value.copy() if isinstance(value, Keyed) else value for name, value in state.items()}


def _show(value: Any) -> str:
    if isinstance(value, Keyed):
        return "{" + ", ".join(f"{k}: {v}" for k, v in sorted(value.items())) + "}" if value else "{}"
    return repr(value) if isinstance(value, str) else str(value)


class Engine:
    def __init__(self, model: ExecutableModel) -> None:
        self.model = model
        self.events = {event.name: event for event in model.events}
        self.fields = {(event.name, item.name): item for event in model.events for item in event.fields}
        self.variables = {variable.name: variable for variable in model.state}
        self.invalid: dict[str, str] = {}
        for rule_id, expressions in self._expressions():
            for expression in expressions:
                try:
                    parse(expression)
                except EvaluationError as exc:
                    self.invalid[rule_id] = str(exc)
        for update in model.updates:
            if update.target not in self.variables:
                self.invalid[update.id] = f"unknown state variable {update.target}"
        for rule in (*model.updates, *model.obligations):
            if rule.on not in self.events:
                self.invalid[rule.id] = f"unknown event {rule.on}"
        for rule_id, reason in self.invalid.items():
            LOG.warning("Ignoring executable rule %s: %s", rule_id, reason)
        self.updates = [u for u in model.updates if u.id not in self.invalid]
        self.obligations = [o for o in model.obligations if o.id not in self.invalid]
        self.invariants = [i for i in model.invariants if i.id not in self.invalid]
        self.rules = {rule.id: rule for rule in (*self.updates, *self.obligations, *self.invariants)}

    def _expressions(self):
        model = self.model
        yield from ((v.name, (v.default,)) for v in model.state)
        yield from ((u.id, tuple(e for e in (u.when, u.key, u.value) if e)) for u in model.updates)
        yield from ((o.id, (o.when, *(f.value for f in o.fields))) for o in model.obligations)
        yield from ((i.id, (i.check,)) for i in model.invariants)

    def _initial(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for variable in self.model.state:
            try:
                default = evaluate(variable.default, Context({}))
            except EvaluationError:
                default = literal(variable.default)
            state[variable.name] = Keyed(default) if variable.keyed else default
        return state

    def _missing(self, result: CaseResult, rule_id: str, event: str, error: MissingInput, step: int) -> None:
        declared = self.fields.get((event, error.field))
        if declared is not None and declared.source is not None:
            # The case omitted a documented input: a generation gap, not a document defect.
            result.problems.append(Problem("error", f"Сценарий не задал поле {error.field}.", (rule_id,), step=step))
            return
        if not any(p.kind == "missing_input" and p.field == error.field and rule_id in p.rule_ids
                   for p in result.problems):
            result.problems.append(Problem(
                "missing_input", f"Правилу {rule_id} нужно поле {error.field} события {event}, "
                                 "но документированный источник его не передаёт.",
                (rule_id,), step=step, field=error.field,
            ))

    def run(self, case: TestCase) -> CaseResult:
        result = CaseResult(case)
        state = self._initial()

        def count(name: str, **filters: Any) -> int:
            return sum(1 for item in result.outputs
                       if item["_name"] == name and all(item.get(k) == v for k, v in filters.items()))

        def max_count(name: str, *group: str) -> int:
            groups: dict[tuple[Any, ...], int] = {}
            for item in result.outputs:
                if item["_name"] == name:
                    values = tuple(item.get(field_name) for field_name in group)
                    groups[values] = groups.get(values, 0) + 1
            return max(groups.values(), default=0)

        for item in case.initial:
            try:
                # Inputs may be bare text such as c1; literal() keeps it as a string.
                value = literal(item.value)
                if item.variable not in state:
                    raise EvaluationError(f"unknown state variable {item.variable}")
                if item.key is None:
                    state[item.variable] = value
                elif not isinstance(state[item.variable], Keyed):
                    raise EvaluationError(f"{item.variable} is not keyed")
                else:
                    state[item.variable][literal(item.key)] = value
            except EvaluationError as exc:
                result.problems.append(Problem("error", f"Начальное состояние: {exc}."))
                return result

        writers: dict[str, list[str]] = {}  # Updates that changed each state variable so far.
        for step, raw in enumerate(case.events, start=1):
            event_type = self.events.get(raw.type)
            if event_type is None:
                result.problems.append(Problem("error", f"Неизвестное событие {raw.type}.", step=step))
                return result
            kinds = {item.name: item.kind for item in event_type.fields}
            event = {a.name: _coerce(literal(a.value), kinds.get(a.name, "text")) for a in raw.fields}
            prev = _snapshot(state)
            ctx = Context(state, prev=prev, event=event, count=count, max_count=max_count)
            changed: list[str] = []
            for update in (u for u in self.updates if u.on == raw.type):
                try:
                    if update.when and not evaluate(update.when, ctx):
                        continue
                    value = evaluate(update.value, ctx)
                    current = state[update.target]
                    key = evaluate(update.key, ctx) if update.key else None
                    old = current[key] if key is not None else current
                    new = value if update.op == "set" else old + value if update.op == "add" else old - value
                    if key is not None:
                        current[key] = new
                    else:
                        state[update.target] = new
                    if new != old:
                        writers.setdefault(update.target, [])
                        if update.id not in writers[update.target]:
                            writers[update.target].append(update.id)
                except MissingInput as exc:
                    self._missing(result, update.id, raw.type, exc, step)
                    continue
                except (EvaluationError, KeyError, TypeError) as exc:
                    result.problems.append(Problem("error", f"{update.id}: {exc}.", (update.id,), step=step))
                    continue
                changed.append(update.id)
            emitted: list[str] = []
            enabling: list[str] = []
            for obligation in (o for o in self.obligations if o.on == raw.type):
                try:
                    if not evaluate(obligation.when, ctx):
                        continue
                    output = {f.name: evaluate(f.value, ctx) for f in obligation.fields}
                except MissingInput as exc:
                    self._missing(result, obligation.id, raw.type, exc, step)
                    continue
                except EvaluationError as exc:
                    result.problems.append(Problem("error", f"{obligation.id}: {exc}.", (obligation.id,), step=step))
                    continue
                result.outputs.append({"_name": obligation.output, "_rule": obligation.id, "_step": step, **output})
                emitted.append(obligation.id)
                # Earlier updates that set the state this obligation read also caused the output.
                for used in names(obligation.when):
                    enabling.extend(writers.get(used.removeprefix("prev."), ()))
            result.fired.extend(changed + emitted)
            for invariant in self.invariants:
                try:
                    holds = evaluate(invariant.check, ctx)
                except MissingInput as exc:
                    self._missing(result, invariant.id, raw.type, exc, step)
                    continue
                except EvaluationError:
                    continue  # An invariant about another event's fields does not apply here.
                if holds:
                    continue
                causes = tuple(dict.fromkeys(emitted + enabling if emitted else changed))
                if any(p.kind == "conflict" and p.rule_ids[:1] == (invariant.id,) for p in result.problems):
                    continue
                result.problems.append(Problem(
                    "conflict", f"После шага {step} нарушено ограничение {invariant.id}; "
                                f"его нарушают правила {', '.join(causes) or 'без явной причины'}.",
                    (invariant.id, *causes), step=step,
                ))

        final = Context(state, prev=state, event=None, count=count, max_count=max_count)
        for expectation in case.expectations:
            try:
                holds = evaluate(expectation.check, final)
            except EvaluationError as exc:
                result.problems.append(Problem("error", f"Ожидание «{expectation.description}»: {exc}."))
                continue
            if not holds:
                result.problems.append(Problem(
                    "mismatch", f"Ожидание не выполнено: {expectation.description}",
                    self._responsible(expectation.check, result), expectation, self._explain(expectation.check, final),
                ))
        return result

    def _responsible(self, check: str, result: CaseResult) -> tuple[str, ...]:
        used = names(check)
        related = [u.id for u in self.updates if u.target in used]
        related += [o.id for o in self.obligations if f"output:{o.output}" in used]
        fired = [rule for rule in dict.fromkeys(result.fired) if rule in related]
        return tuple(fired or related)

    @staticmethod
    def _explain(check: str, ctx: Context) -> str:
        node = parse(check).body
        parts = node.comparators[:1] if isinstance(node, ast.Compare) else []
        operands = [node.left, *parts] if isinstance(node, ast.Compare) else [node]
        shown = []
        for operand in operands:
            if isinstance(operand, ast.Constant):
                continue
            text = ast.unparse(operand)
            try:
                shown.append(f"{text} = {_show(_evaluate_node(operand, ctx))}")
            except EvaluationError:
                continue
        return "; ".join(shown)


def _evaluate_node(node: ast.AST, ctx: Context) -> Any:
    return evaluate(ast.unparse(node), ctx)


SPECIAL = {"count", "max_count", "True", "False", "None"}


def diagnose(model: ExecutableModel) -> list[str]:
    """Static checks fed back to the model so it can repair its own translation."""
    from .executable_model import FUNCTIONS

    problems: list[str] = []
    variables = {variable.name: variable.keyed for variable in model.state}
    fields = {event.name: {item.name for item in event.fields} for event in model.events}

    def check(rule_id: str, expression: str, event: str | None) -> None:
        try:
            tree = parse(expression)
        except EvaluationError as exc:
            problems.append(f"{rule_id}: {exc}")
            return
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name) and not isinstance(parents.get(node), ast.Attribute):
                if isinstance(parents.get(node), ast.Call) and parents[node].func is node:
                    if node.id not in FUNCTIONS and node.id not in SPECIAL:
                        problems.append(f"{rule_id}: unknown function {node.id}() in `{expression}`")
                    continue
                if node.id in SPECIAL:
                    continue
                name = node.id
            elif isinstance(node, ast.Attribute) and node.value.id == "prev":
                name = node.attr
            elif isinstance(node, ast.Attribute) and node.value.id == "event":
                if event is not None and node.attr not in fields.get(event, set()):
                    problems.append(f"{rule_id}: event {event} has no field {node.attr}; declare it in the event")
                continue
            if name is None:
                continue
            if name not in variables:
                problems.append(f"{rule_id}: unknown state variable {name} in `{expression}`")
                continue
            indexed = isinstance(parents.get(node), ast.Subscript) and parents[node].value is node
            if variables[name] and not indexed:
                problems.append(f"{rule_id}: {name} is keyed, so read it as {name}[key] in `{expression}`")
            elif not variables[name] and indexed:
                problems.append(f"{rule_id}: {name} is not keyed, so do not index it in `{expression}`")

    for variable in model.state:
        check(variable.name, variable.default, None)
    for update in model.updates:
        if update.on not in fields:
            problems.append(f"{update.id}: unknown event {update.on}")
        if update.target not in variables:
            problems.append(f"{update.id}: unknown target {update.target}")
        elif variables[update.target] != (update.key is not None):
            problems.append(f"{update.id}: target {update.target} " +
                            ("needs a key" if variables[update.target] else "is not keyed, so key must be null"))
        for expression in (update.when, update.key, update.value):
            if expression:
                check(update.id, expression, update.on)
    for obligation in model.obligations:
        if obligation.on not in fields:
            problems.append(f"{obligation.id}: unknown event {obligation.on}")
        for expression in (obligation.when, *(item.value for item in obligation.fields)):
            check(obligation.id, expression, obligation.on)
    for invariant in model.invariants:
        check(invariant.id, invariant.check, None)
    output_fields: dict[str, set[str]] = {}
    for obligation in model.obligations:
        output_fields.setdefault(obligation.output, set()).update(item.name for item in obligation.fields)
    for rule_id, expression in [(i.id, i.check) for i in model.invariants] + [
            (o.id, o.when) for o in model.obligations]:
        try:
            tree = parse(expression)
        except EvaluationError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "max_count"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                continue
            known = output_fields.get(node.args[0].value)
            if known is None:
                problems.append(f"{rule_id}: no obligation emits output {node.args[0].value!r}")
                continue
            for argument in node.args[1:]:
                if not (isinstance(argument, ast.Constant) and argument.value in known):
                    problems.append(f"{rule_id}: max_count groups by output field names {sorted(known)} given as "
                                    f"strings; add the field to the obligation's fields if needed, in `{expression}`")
    return list(dict.fromkeys(problems))


TIME = re.compile(r"\b\d{1,2}:\d{2}\b")


def diagnose_cases(cases: tuple[TestCase, ...], model: ExecutableModel,
                   rows: dict[str, str] | None = None) -> list[str]:
    """Static checks of generated cases, fed back for repair before results are trusted."""
    problems: list[str] = []
    variables = {variable.name: variable.keyed for variable in model.state}
    outputs = {obligation.output for obligation in model.obligations}
    events = {event.name: event for event in model.events}
    for case in cases:
        for expectation in case.expectations:
            try:
                tree = parse(expectation.check)
            except EvaluationError as exc:
                problems.append(f"{case.id}: {exc}")
                continue
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in {"count", "max_count"} and node.args
                        and isinstance(node.args[0], ast.Constant) and node.args[0].value not in outputs):
                    problems.append(f"{case.id}: no rule emits output {node.args[0].value!r}; "
                                    f"outputs are {sorted(outputs)}")
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "key(" in node.value:
                    problems.append(f"{case.id}: write key(...) as an expression, not inside a string, "
                                    f"in `{expectation.check}`")
                if isinstance(node, ast.Name) and node.id in variables and variables[node.id]:
                    parent = parents.get(node)
                    if not (isinstance(parent, ast.Subscript) and parent.value is node):
                        problems.append(f"{case.id}: {node.id} is keyed, so read it as {node.id}[key]")
                if (isinstance(node, ast.Name) and node.id not in variables and node.id not in SPECIAL
                        and not isinstance(parents.get(node), ast.Call)):
                    problems.append(f"{case.id}: unknown name {node.id} in `{expectation.check}`")
        for item in case.initial:
            if item.variable not in variables:
                problems.append(f"{case.id}: unknown state variable {item.variable}")
        if case.kind == "acceptance" and rows is not None and case.id in rows:
            supplied = " ".join(a.value for raw in case.events for a in raw.fields)
            for moment in sorted(set(TIME.findall(rows[case.id]))):
                if moment.zfill(5) not in supplied and moment not in supplied:
                    problems.append(f"{case.id}: the row states the time {moment} but no event input contains it; "
                                    "copy every stated time into the field for that moment, without adapting it")
            # A row naming several clock times describes distinct moments of one operation.
            stated = len(set(TIME.findall(rows[case.id])))
            for raw in case.events:
                event = events.get(raw.type)
                timestamps = [f.name for f in event.fields if f.kind == "timestamp"] if event else []
                if stated > len(timestamps) and timestamps:
                    problems.append(
                        f"{case.id}: the row states {stated} different times but event {raw.type} has only "
                        f"{len(timestamps)} timestamp field(s) {timestamps}; give the event a separate field for "
                        "each moment the row names, such as when the operation was made and when it was processed")
    return list(dict.fromkeys(problems))
