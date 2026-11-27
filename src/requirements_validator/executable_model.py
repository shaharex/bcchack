"""A small executable model of a specification's behavioral rules.

The model translates rules into this vocabulary. Python, not the model, then
computes what each rule requires for concrete scenarios. Expressions use a
restricted Python syntax that is parsed and evaluated without ``eval``.
"""

import ast
import operator
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, Field

from .models import Anchor, Schema, Text

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAX_EXPRESSION = 400


def identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("expected an ASCII identifier")
    return value


Name = Annotated[str, AfterValidator(identifier)]
Expression = Annotated[str, Field(min_length=1, max_length=MAX_EXPRESSION)]


class InputField(Schema):
    name: Name
    kind: Literal["number", "text", "timestamp", "flag"]
    # Where the document supplies this input. Null marks an input a rule needs
    # although no documented contract provides it.
    source: Anchor | None


class EventType(Schema):
    name: Name
    description: Text
    fields: tuple[InputField, ...]
    source: Anchor


class StateVariable(Schema):
    name: Name
    keyed: bool
    default: Expression
    description: Text


class Update(Schema):
    id: Name
    on: Name
    when: Expression | None = None
    target: Name
    key: Expression | None = None
    op: Literal["set", "add", "subtract"]
    value: Expression
    source: Anchor


class OutputField(Schema):
    name: Name
    value: Expression


class Obligation(Schema):
    id: Name
    on: Name
    when: Expression
    output: Name
    fields: tuple[OutputField, ...] = ()
    source: Anchor


class Invariant(Schema):
    id: Name
    check: Expression
    source: Anchor


class SkippedPassage(Schema):
    source: Anchor
    reason: Text


class ExecutableModel(Schema):
    rationale: Text
    events: tuple[EventType, ...]
    state: tuple[StateVariable, ...]
    updates: tuple[Update, ...]
    obligations: tuple[Obligation, ...]
    invariants: tuple[Invariant, ...]
    # Rule sentences deliberately left out of the model, with the reason.
    skipped: tuple[SkippedPassage, ...] = ()


class Assignment(Schema):
    name: Name
    value: Expression


class InitialValue(Schema):
    variable: Name
    key: Expression | None = None
    value: Expression


class CaseEvent(Schema):
    type: Name
    fields: tuple[Assignment, ...]


class Expectation(Schema):
    check: Expression
    description: Text


class TestCase(Schema):
    id: Name
    kind: Literal["acceptance", "probe"]
    description: Text
    # The acceptance row for an acceptance case. Probes are generated inputs.
    source: Anchor | None
    initial: tuple[InitialValue, ...] = ()
    events: tuple[CaseEvent, ...] = Field(min_length=1)
    expectations: tuple[Expectation, ...] = ()


class TestCases(Schema):
    cases: tuple[TestCase, ...]


class EvaluationError(ValueError):
    """An expression is unsupported or cannot be evaluated."""


class MissingInput(EvaluationError):
    def __init__(self, field: str) -> None:
        super().__init__(f"event field {field} is not supplied")
        self.field = field


class Keyed(dict):
    """A state variable with one value per key, such as a card and month."""

    def __init__(self, default: Any) -> None:
        super().__init__()
        self.default = default

    def __missing__(self, key: Any) -> Any:
        return self.default

    def copy(self) -> "Keyed":
        clone = Keyed(self.default)
        clone.update(self)
        return clone


def _moment(value: Any, zone: str) -> datetime:
    if not isinstance(value, str):
        raise EvaluationError("timestamp must be an ISO 8601 string")
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise EvaluationError(f"unknown time zone {zone}") from exc
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationError(f"invalid timestamp {value}") from exc
    # A timestamp without an offset is local time in the named zone.
    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)


def _month(value: Any, zone: str = "UTC") -> str:
    return _moment(value, zone).strftime("%Y-%m")


def _date(value: Any, zone: str = "UTC") -> str:
    return _moment(value, zone).strftime("%Y-%m-%d")


def _minutes_between(start: Any, end: Any) -> float:
    return (_moment(end, "UTC").astimezone(UTC) - _moment(start, "UTC").astimezone(UTC)).total_seconds() / 60


def _key(*parts: Any) -> str:
    return "|".join(str(part) for part in parts)


BINARY: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
}
COMPARE: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}
FUNCTIONS: dict[str, Callable[..., Any]] = {
    "month": _month, "date": _date, "minutes_between": _minutes_between, "key": _key,
    "min": min, "max": max, "abs": abs, "int": int, "round": round, "len": len,
}


class Context:
    def __init__(self, state: Mapping[str, Any], *, prev: Mapping[str, Any] | None = None,
                 event: Mapping[str, Any] | None = None,
                 count: Callable[..., int] | None = None,
                 max_count: Callable[..., int] | None = None) -> None:
        self.state = state
        self.prev = prev if prev is not None else state
        self.event = event
        self.count = count or (lambda *args, **kwargs: 0)
        # Largest number of outputs with one name sharing the same values of the given fields.
        self.max_count = max_count or (lambda *args: 0)


def parse(expression: str) -> ast.Expression:
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise EvaluationError(f"invalid expression: {expression}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and (
            not isinstance(node.value, ast.Name) or node.value.id not in {"event", "prev"}
        ):
            raise EvaluationError("attributes are limited to event.<field> and prev.<variable>")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise EvaluationError("private names are not allowed")
    return tree


def names(expression: str) -> set[str]:
    """State variables, event fields and output names an expression reads."""
    found = set()
    for node in ast.walk(parse(expression)):
        if isinstance(node, ast.Attribute):
            found.add(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.Name) and node.id not in FUNCTIONS and node.id not in {"event", "prev", "count", "max_count"}:
            found.add(node.id)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"count", "max_count"}
              and node.args and isinstance(node.args[0], ast.Constant)):
            found.add(f"output:{node.args[0].value}")
    return found


def evaluate(expression: str, context: Context) -> Any:
    return _eval(parse(expression).body, context)


def _eval(node: ast.AST, ctx: Context) -> Any:
    match node:
        case ast.Constant(value=value) if isinstance(value, (int, float, str, bool)) or value is None:
            return value
        case ast.Name(id=name):
            if name in ctx.state:
                return ctx.state[name]
            if name in {"true", "false"}:
                return name == "true"
            raise EvaluationError(f"unknown name {name}")
        case ast.Attribute(value=ast.Name(id="event"), attr=field):
            if ctx.event is None or field not in ctx.event:
                raise MissingInput(field)
            return ctx.event[field]
        case ast.Attribute(value=ast.Name(id="prev"), attr=name):
            if name not in ctx.prev:
                raise EvaluationError(f"unknown name {name}")
            return ctx.prev[name]
        case ast.Subscript(value=target, slice=index):
            container = _eval(target, ctx)
            if not isinstance(container, (dict, list, tuple, str)):
                raise EvaluationError("only state maps and lists can be indexed")
            try:
                return container[_eval(index, ctx)]
            except (KeyError, IndexError, TypeError) as exc:
                raise EvaluationError("invalid index") from exc
        case ast.BinOp(left=left, op=op, right=right) if type(op) in BINARY:
            try:
                return BINARY[type(op)](_eval(left, ctx), _eval(right, ctx))
            except (TypeError, ZeroDivisionError) as exc:
                raise EvaluationError(str(exc)) from exc
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            return not _eval(operand, ctx)
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return -_eval(operand, ctx)
        case ast.BoolOp(op=ast.And(), values=values):
            return all(_eval(value, ctx) for value in values)
        case ast.BoolOp(op=ast.Or(), values=values):
            return any(_eval(value, ctx) for value in values)
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            current = _eval(left, ctx)
            for op, comparator in zip(ops, comparators, strict=True):
                if type(op) not in COMPARE:
                    raise EvaluationError("unsupported comparison")
                following = _eval(comparator, ctx)
                try:
                    if not COMPARE[type(op)](current, following):
                        return False
                except TypeError as exc:
                    raise EvaluationError(str(exc)) from exc
                current = following
            return True
        case ast.IfExp(test=test, body=body, orelse=orelse):
            return _eval(body, ctx) if _eval(test, ctx) else _eval(orelse, ctx)
        case ast.Tuple(elts=items) | ast.List(elts=items):
            return tuple(_eval(item, ctx) for item in items)
        case ast.Call(func=ast.Name(id="count"), args=args, keywords=keywords):
            return ctx.count(*(_eval(a, ctx) for a in args), **{k.arg: _eval(k.value, ctx) for k in keywords})
        case ast.Call(func=ast.Name(id="max_count"), args=args, keywords=[]):
            return ctx.max_count(*(_eval(a, ctx) for a in args))
        case ast.Call(func=ast.Name(id=name), args=args, keywords=keywords) if name in FUNCTIONS:
            if any(keyword.arg is None for keyword in keywords):
                raise EvaluationError("unsupported argument unpacking")
            try:
                return FUNCTIONS[name](*(_eval(a, ctx) for a in args), **{k.arg: _eval(k.value, ctx) for k in keywords})
            except EvaluationError:
                raise
            except (TypeError, ValueError) as exc:
                raise EvaluationError(f"{name}: {exc}") from exc
    raise EvaluationError(f"unsupported expression element {type(node).__name__}")


def literal(value: str) -> Any:
    """Evaluate a case input; bare text that is not an expression stays text."""
    try:
        return evaluate(value, Context({}))
    except EvaluationError:
        return value
