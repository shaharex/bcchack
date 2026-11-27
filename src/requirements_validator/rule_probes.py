"""Targeted probes for two defect classes that broad review tends to miss.

1. Output cap versus override. One rule caps how often an output may occur
   ("не более одного push за месяц"), another rule demands the output again
   "независимо от" prior history. Python finds both kinds of sentences, the model
   only decides whether one concrete sequence makes them collide.
2. Read demand without a read operation. A consumer reads values "через" a
   documented interface, but every operation of that interface is named as a
   write ("saveLimit", "update", "Сохранение"). Python inventories the operations,
   the model only decides whether any of them returns what the consumer reads.

Python picks every quotation from the source, so the evidence always resolves.
The model answers narrow yes/no questions with short Russian explanations. All
proposals still pass the independent verifier like any other finding.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from .models import ParsedDocument, ProposedEvidence, QualityCriterion, Requirement, Scenario, Schema, Text
from .ports import StructuredLLM, StructuredResponseError
from .prompts import SOURCE_RULES, SUBJECT_RULES
from .reviewers import ProposedFinding

LOG = logging.getLogger(__name__)

SENTENCE = re.compile(r"[^.!?;\n]+[.!?;]?")

CAP = re.compile(
    r"(?<![\w-])(?:максимум|не более|не больше|не чаще|не свыше|только один|только одно|лишь один|"
    r"один раз|однократно|единожды|at most|no more than|not more than|only once|at most once|"
    r"maximum of|up to one)(?![\w-])", re.IGNORECASE)
OVERRIDE = re.compile(
    r"(?<![\w-])(?:независимо от|вне зависимости от|в любом случае|безотносительно|без учёта|без учета|"
    r"regardless of|irrespective of|in any case|whether or not)(?![\w-])", re.IGNORECASE)

READ = re.compile(
    r"(?<![\w-])(?:чита\w*|прочит\w*|чтени\w*|получ\w*|просматрива\w*|просмотр\w*|видн\w*|"
    r"отображ\w*|показыва\w*|выгруж\w*|reads?|reading|retriev\w*|fetch\w*|displays?|views?|lists?)(?![\w-])",
    re.IGNORECASE)
# Endpoints written as /segment/segment, with or without a base-URL variable in front.
ENDPOINT = re.compile(r"(?<![\w/.:])/[A-Za-z_{][\w{}\-]*(?:/[\w{}\-.]+)*")
METHOD = re.compile(r"(?<![\w-])(GET|POST|PUT|PATCH|DELETE)(?![\w-])")
WRITE_WORDS = {"save", "set", "update", "create", "add", "put", "post", "patch", "delete", "remove", "change",
               "edit", "store", "submit", "register", "write", "upsert", "insert", "assign", "enable", "disable",
               "toggle", "apply", "reset"}
READ_WORDS = {"get", "read", "list", "fetch", "find", "search", "load", "show", "view", "query", "retrieve",
              "lookup", "status", "info", "details", "current", "describe", "export"}


@dataclass(frozen=True)
class Passage:
    block_id: str
    quote: str
    start: int
    occurrence: int
    requirement_id: str | None
    section: str

    def evidence(self) -> ProposedEvidence:
        return ProposedEvidence(block_id=self.block_id, quote=self.quote, occurrence=self.occurrence,
                                requirement_id=self.requirement_id)

    def payload(self, number: int) -> dict:
        return {"index": number, "section": self.section, "quote": self.quote}


def _occurrence(text: str, quote: str, position: int) -> int:
    count, start = 0, text.find(quote)
    while 0 <= start <= position:
        count += 1
        start = text.find(quote, start + 1)
    return count


def _owner(requirements: tuple[Requirement, ...], block_id: str, start: int, end: int) -> str | None:
    """The extracted unit whose atomic span holds the quotation, preferring the narrowest."""
    owners = [r for r in requirements if r.source.location.block_id == block_id
              and r.source.location.start <= start and end <= r.source.location.end]
    if owners:
        return min(owners, key=lambda r: r.source.location.end - r.source.location.start).id
    return None


def _sentences(document: ParsedDocument, requirements: tuple[Requirement, ...]) -> list[Passage]:
    passages = []
    for block in document.blocks:
        for match in SENTENCE.finditer(block.text):
            raw = match[0]
            quote = raw.strip(" \t*|`#")
            if len(quote) < 8:
                continue
            start = match.start() + raw.index(quote)
            # Quote the smaller extracted unit when the sentence is one, so the finding links to it.
            owner = _owner(requirements, block.id, start, start + len(quote))
            passages.append(Passage(block.id, quote, start, _occurrence(block.text, quote, start), owner,
                                    " → ".join(block.section)))
    return passages


def _split_identifier(name: str) -> list[str]:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", words) if w]


def operation_kind(endpoint: str) -> Literal["read", "write", "unknown"]:
    """Classify an endpoint by the verb in its last named path segment."""
    segments = [s for s in endpoint.strip("/").split("/") if s and not s.startswith("{")]
    for segment in reversed(segments):
        words = _split_identifier(segment)
        if not words:
            continue
        if words[0] in WRITE_WORDS:
            return "write"
        if words[0] in READ_WORDS:
            return "read"
    return "unknown"


@dataclass(frozen=True)
class Operation:
    endpoint: str
    kind: str
    method: str | None
    passage: Passage


def inventory_operations(document: ParsedDocument, requirements: tuple[Requirement, ...]) -> list[Operation]:
    operations: dict[str, Operation] = {}
    for block in document.blocks:
        for line_match in re.finditer(r"[^\n]+", block.text):
            line = line_match[0]
            for match in ENDPOINT.finditer(line):
                endpoint = match[0].rstrip(".")
                if endpoint in operations or len(endpoint) < 3:
                    continue
                method = METHOD.search(line)
                quote = line
                if line.lstrip().startswith("|"):
                    # Quote only the table cell that declares the endpoint.
                    quote = next(cell for cell in line.split("|") if match[0] in cell)
                quote = quote.strip(" \t|*")
                start = line_match.start() + line.index(quote)
                passage = Passage(block.id, quote, start, _occurrence(block.text, quote, start),
                                  _owner(requirements, block.id, start, start + len(quote)),
                                  " → ".join(block.section))
                operations[endpoint] = Operation(endpoint, operation_kind(endpoint),
                                                 method[1] if method else None, passage)
    return list(operations.values())


class CapVerdict(Schema):
    pair: int = Field(ge=0)
    outcome: Literal["conflict", "compatible", "unrelated"]
    scenario: Text | None = None
    cap_outcome: Text | None = None
    override_outcome: Text | None = None
    subject: Text | None = None
    consequence: Text | None = None
    recommendation: Text | None = None


class CapVerdicts(Schema):
    rationale: Text
    verdicts: tuple[CapVerdict, ...]


class ReadVerdict(Schema):
    demand: int = Field(ge=0)
    served_by: Text | None = Field(description="Documented endpoint that returns the read values, or null.")
    gap: bool
    missing_values: Text | None = None
    subject: Text | None = None
    consequence: Text | None = None
    recommendation: Text | None = None


class ReadVerdicts(Schema):
    rationale: Text
    verdicts: tuple[ReadVerdict, ...]


CAP_RULES = SOURCE_RULES + """
Each numbered pair has a cap passage that limits how many times an output may occur
in a scope (for example per entity per period) and an override passage that requires
an output "regardless of" some history. Use the whole document for context.
For each pair, build one concrete sequence of events in the SAME scope and period:
the capped output has already happened once, then the condition named in the override
passage occurs (for example a setting changes and the trigger fires again).
Decide what each passage requires in that exact situation.
- conflict: the override passage requires the output and the cap passage forbids it,
  and no passage in the document states which rule takes precedence.
- compatible: both can be satisfied, or the document explicitly resolves precedence.
- unrelated: the passages concern different outputs or scopes.
For conflict fill scenario (the concrete sequence with numbers taken from the document),
cap_outcome (what the cap passage requires: e.g. "второй push запрещён"),
override_outcome (what the override passage requires: e.g. "второй push обязателен"),
subject, consequence for implementation and testing, and a recommendation asking the
stakeholder to choose precedence. Write in Russian. Return one verdict per pair.""" + SUBJECT_RULES

READ_RULES = SOURCE_RULES + """
operations lists every endpoint documented in the specification with the line that
declares it, its HTTP method if given and a naive verb classification from its name.
Each numbered demand is a passage where a consumer (a client, screen or service)
reads, shows or receives values. For each demand decide whether some documented
operation returns those values to that consumer.
- A write/save operation whose response merely echoes the values just submitted does
  NOT serve a demand to read the current stored state (for example to prefill a
  settings screen before the user has saved anything).
- Ignore demands that are fully served by an event, message or another documented
  interface, or that concern a system outside this specification's contracts.
- Set gap=true only when the passage expects the read through the documented interface,
  the same consumer that calls it, or a screen that must show stored values, and no
  documented operation returns them. Name the missing values.
For a gap fill subject, consequence for implementation and testing and a recommendation
to add or document the read operation, without prescribing its URL or HTTP method.
Write in Russian. Return one verdict per demand.""" + SUBJECT_RULES


def _scope_words(text: str) -> set[str]:
    return {w[:5].lower() for w in re.findall(r"\w{4,}", text)}


class RuleProbes:
    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm

    def review(self, document: ParsedDocument, requirements: tuple[Requirement, ...]) -> tuple[ProposedFinding, ...]:
        passages = _sentences(document, requirements)
        findings: list[ProposedFinding] = []
        for probe in (self.cap_overrides, self.read_operations):
            try:
                findings.extend(probe(document, requirements, passages))
            except StructuredResponseError as exc:
                LOG.warning("Targeted probe %s returned an invalid answer: %s", probe.__name__, exc)
        return tuple(findings)

    def cap_overrides(self, document: ParsedDocument, requirements: tuple[Requirement, ...],
                      passages: list[Passage]) -> list[ProposedFinding]:
        caps = [p for p in passages if CAP.search(p.quote)]
        overrides = [p for p in passages if OVERRIDE.search(p.quote)]
        pairs = [(c, o) for c in caps for o in overrides
                 if (c.block_id, c.start) != (o.block_id, o.start)
                 # Keep pairs that talk about the same thing: shared word stems.
                 and len(_scope_words(c.quote) & _scope_words(o.quote)) >= 1]
        if not pairs:
            return []
        pairs = pairs[:8]
        payload = {"document": document.model_dump(),
                   "pairs": [{"pair": i, "cap": c.quote, "cap_section": c.section,
                              "override": o.quote, "override_section": o.section}
                             for i, (c, o) in enumerate(pairs)]}
        result = self.llm.generate(CAP_RULES, json.dumps(payload, ensure_ascii=False), CapVerdicts)
        findings = []
        for verdict in result.verdicts:
            if verdict.outcome != "conflict" or not 0 <= verdict.pair < len(pairs):
                continue
            if not all((verdict.scenario, verdict.cap_outcome, verdict.override_outcome, verdict.subject,
                        verdict.recommendation)):
                continue
            if verdict.cap_outcome.casefold().split() == verdict.override_outcome.casefold().split():
                continue
            cap, override = pairs[verdict.pair]
            explanation = " ".join(s.strip() for s in (
                verdict.scenario, f"По ограничению — {verdict.cap_outcome.strip().rstrip('.')}.",
                f"По правилу, действующему «независимо» от истории, — {verdict.override_outcome.strip().rstrip('.')}.",
                verdict.consequence or "",
            ) if s)
            findings.append(ProposedFinding(
                criterion=QualityCriterion.CONSISTENCY, subject=verdict.subject, explanation=explanation,
                recommendation=verdict.recommendation, evidence=(cap.evidence(), override.evidence()),
                scenario=Scenario(relationship="contradiction", description=verdict.scenario,
                                  expected_a=verdict.cap_outcome, expected_b=verdict.override_outcome,
                                  evidence_a=0, evidence_b=1),
            ))
        return findings

    def read_operations(self, document: ParsedDocument, requirements: tuple[Requirement, ...],
                        passages: list[Passage]) -> list[ProposedFinding]:
        operations = inventory_operations(document, requirements)
        if not operations or any(op.kind == "read" for op in operations):
            return []
        declared = {op.passage.block_id for op in operations}
        demands = [p for p in passages if READ.search(p.quote) and p.block_id not in declared]
        if not demands:
            return []
        demands = demands[:12]
        payload = {
            "document": document.model_dump(),
            "operations": [{"endpoint": op.endpoint, "method": op.method, "name_suggests": op.kind,
                            "declared_in": op.passage.quote} for op in operations],
            "demands": [p.payload(i) for i, p in enumerate(demands)],
        }
        result = self.llm.generate(READ_RULES, json.dumps(payload, ensure_ascii=False), ReadVerdicts)
        findings = []
        contract = operations[0].passage
        for verdict in result.verdicts:
            if not verdict.gap or verdict.served_by or not 0 <= verdict.demand < len(demands):
                continue
            if not (verdict.subject and verdict.recommendation):
                continue
            demand = demands[verdict.demand]
            listed = ", ".join(f"{op.method + ' ' if op.method else ''}{op.endpoint}" for op in operations)
            explanation = " ".join(s for s in (
                f"Потребитель должен получать {verdict.missing_values or 'сохранённые значения'}, "
                f"но в контракте описаны только операции записи: {listed}.",
                verdict.consequence or "",
            ) if s)
            findings.append(ProposedFinding(
                criterion=QualityCriterion.COMPLETENESS, subject=verdict.subject, explanation=explanation,
                recommendation=verdict.recommendation, evidence=(demand.evidence(), contract.evidence()),
            ))
        return findings
