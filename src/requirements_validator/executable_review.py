"""Executable check: the model translates rules, Python runs the scenarios.

Every rule, event and acceptance row must carry an exact source quotation. A
finding is proposed only from a computed result: an acceptance row that the
rules do not reproduce, an output one rule requires and another forbids, or an
input a rule needs that no documented contract supplies. The aggregator then
grounds and independently verifies each proposal like any other.
"""

import json
import logging
from dataclasses import dataclass

from .executable_engine import CaseResult, Engine, Problem, diagnose, diagnose_cases
from .executable_model import Anchor, ExecutableModel, TestCase, TestCases
from .models import (
    Citation, ExecutionRow, SourceLocation, ParsedDocument, ProposedEvidence, QualityCriterion as C, Requirement, Scenario,
)
from .ports import StructuredLLM, StructuredResponseError
from .prompts import EXECUTABLE_CASES_REPAIR, EXECUTABLE_CASES_RULES, EXECUTABLE_MODEL_RULES, EXECUTABLE_REPAIR
from .reviewers import CrossRequirementReviewer, ProposedFinding, SpecificationReview
from .source import EvidenceError, SourceIndex, contains

LOG = logging.getLogger(__name__)
MAX_CASES = 24
MODEL_REPAIRS = 2
STATUS_TEXT = {
    "pass": "Результат совпадает с правилами.",
    "conflict": "Правила требуют несовместимых результатов.",
    "mismatch": "Пример документа не следует из правил.",
    "missing_input": "Правилу не хватает входных данных.",
    "error": "Сценарий не выполнен.",
}


@dataclass(frozen=True)
class ExecutionReview:
    findings: SpecificationReview
    rows: tuple[ExecutionRow, ...] | None


EMPTY = ExecutionReview(SpecificationReview(findings=()), None)


def _normalized(text: str) -> str:
    return " ".join(text.replace("|", " ").split()).casefold()


def resolve_row(anchor: Anchor, index: SourceIndex) -> Citation | None:
    """Resolve an acceptance row exactly, or else the one source line it paraphrases by spacing only."""
    try:
        return index.resolve(anchor)
    except EvidenceError:
        pass
    block = index.blocks.get(anchor.block_id)
    wanted = _normalized(anchor.quote)
    if block is None or not wanted:
        return None
    offset, matches = 0, []
    for line in block.text.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        if wanted in _normalized(text) and text.strip():
            matches.append(Citation(quote=text, location=SourceLocation(
                block_id=block.id, start=offset, end=offset + len(text))))
        offset += len(line)
    return matches[0] if len(matches) == 1 else None


def _evidence(citation: Citation) -> ProposedEvidence:
    # The aggregator links the quotation to an extracted requirement when one contains it.
    return ProposedEvidence(block_id=citation.location.block_id, quote=citation.quote, requirement_id=None)


class ExecutableReviewer:
    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm

    def _generate(self, instruction: str, payload: str, schema):
        try:
            return self.llm.generate(instruction, payload, schema)
        except StructuredResponseError:
            hint = "\nReturn JSON that matches the schema exactly. Every source is an exact quotation or null."
            return self.llm.generate(instruction + hint, payload, schema)

    def review(self, document: ParsedDocument, requirements: tuple[Requirement, ...]) -> ExecutionReview:
        base = CrossRequirementReviewer._payload(document, requirements)
        try:
            model = self._generate(EXECUTABLE_MODEL_RULES, json.dumps(base, ensure_ascii=False), ExecutableModel)
        except StructuredResponseError:
            LOG.warning("Executable model could not be built; the executable check is skipped.")
            return EMPTY
        index = SourceIndex(document)
        model = self._repair_model(model, base, index, requirements, rounds=MODEL_REPAIRS)
        if not (model.updates or model.obligations):
            return ExecutionReview(SpecificationReview(findings=()), ())
        cases = self._cases(model, base)
        if cases is None:
            return ExecutionReview(SpecificationReview(findings=()), ())
        # A case can show that the model lacks an input the document distinguishes.
        gaps = [d for d in diagnose_cases(cases, model, self._rows(cases, index)) if "timestamp field" in d]
        if gaps:
            model = self._repair_model(model, base, index, requirements, rounds=1, extra=gaps)
            cases = self._cases(model, base) or cases
        model, citations, _ = self._ground(model, index)
        engine = Engine(model)
        runs = self._run(cases, engine, index)
        rows = self._rows(cases, index)
        unmatched = [f"{case.id}: source must copy one acceptance row or example of the document verbatim"
                     for case in cases if case.kind == "acceptance" and case.id not in rows]
        problems = unmatched + diagnose_cases(cases, model, rows) + [
            f"{result.case.id}: {problem.message}" for result, _ in runs
            for problem in result.problems if problem.kind == "error"]
        if problems:
            LOG.info("Repairing executable test cases (%d diagnostics)", len(problems))
            repair = {**base, "model": model.model_dump(mode="json"),
                      "previous_cases": [c.model_dump(mode="json") for c in cases],
                      "diagnostics": list(dict.fromkeys(problems))}
            try:
                cases = self._generate(EXECUTABLE_CASES_RULES + EXECUTABLE_CASES_REPAIR,
                                       json.dumps(repair, ensure_ascii=False), TestCases).cases[:MAX_CASES]
                runs = self._run(cases, engine, index)
            except StructuredResponseError:
                pass
        return ExecutionReview(
            SpecificationReview(findings=self._findings(runs, engine, citations, requirements)),
            tuple(self._row(result) for result, _ in runs),
        )

    def _repair_model(self, model: ExecutableModel, base: dict, index: SourceIndex,
                      requirements: tuple[Requirement, ...], *, rounds: int,
                      extra: list[str] | None = None) -> ExecutableModel:
        for _ in range(rounds):
            if not (model.updates or model.obligations or model.invariants):
                break  # Nothing executable in this document; a repair cannot help.
            grounded, citations, dropped = self._ground(model, index)
            diagnostics = (extra or []) + dropped + diagnose(grounded) + self._untranslated(citations, requirements)
            extra = None
            if not diagnostics:
                break
            LOG.info("Repairing the executable model (%d diagnostics)", len(diagnostics))
            repair = {**base, "previous_model": model.model_dump(mode="json"), "diagnostics": diagnostics}
            try:
                model = self._generate(EXECUTABLE_MODEL_RULES + EXECUTABLE_REPAIR,
                                       json.dumps(repair, ensure_ascii=False), ExecutableModel)
            except StructuredResponseError:
                break
        return model

    def _cases(self, model: ExecutableModel, base: dict) -> tuple[TestCase, ...] | None:
        grounded = model.model_dump(mode="json")
        try:
            return self._generate(EXECUTABLE_CASES_RULES, json.dumps({**base, "model": grounded}, ensure_ascii=False),
                                  TestCases).cases[:MAX_CASES]
        except StructuredResponseError:
            LOG.warning("Executable test cases could not be generated; the executable check is skipped.")
            return None

    @staticmethod
    def _rows(cases: tuple[TestCase, ...], index: SourceIndex) -> dict[str, str]:
        rows = {}
        for case in cases:
            if case.kind == "acceptance" and case.source is not None:
                row = resolve_row(case.source, index)
                if row is not None:
                    rows[case.id] = row.quote
        return rows

    @staticmethod
    def _untranslated(citations: dict[str, Citation], requirements: tuple[Requirement, ...]) -> list[str]:
        """Numbered requirements that no rule translates."""
        spans = [c.location for c in citations.values()]
        missing = []
        for requirement in requirements:
            if requirement.original_id is None or requirement.kind not in {"atomic_requirement", "compound_requirement"}:
                continue
            location = requirement.source.location
            if not any(s.block_id == location.block_id and s.start < location.end and location.start < s.end
                       for s in spans):
                missing.append(f"Requirement {requirement.original_id.quote} is not translated: "
                               f"\"{requirement.source.quote}\". Translate it unless it has no computable effect.")
        return missing

    @staticmethod
    def _run(cases: tuple[TestCase, ...], engine: Engine, index: SourceIndex) -> list[tuple[CaseResult, Citation | None]]:
        runs: list[tuple[CaseResult, Citation | None]] = []
        for case in cases:
            source = None
            if case.kind == "acceptance":
                source = resolve_row(case.source, index) if case.source else None
                if source is None:
                    LOG.warning("Skipping acceptance case %s without an exact source row.", case.id)
                    continue
            else:
                # Only computed rule interactions count for probes, not the model's own expectations.
                case = case.model_copy(update={"expectations": ()})
            runs.append((engine.run(case), source))
        return runs

    @staticmethod
    def _ground(model: ExecutableModel, index: SourceIndex) -> tuple[ExecutableModel, dict[str, Citation], list[str]]:
        """Drop rules whose quotation is not in the source; keep citations for the rest."""
        citations: dict[str, Citation] = {}
        dropped: list[str] = []

        def resolve(anchor: Anchor | None) -> Citation | None:
            if anchor is None:
                return None
            try:
                return index.resolve(anchor)
            except EvidenceError:
                return None

        events = []
        for event in model.events:
            event_source = resolve(event.source)
            if event_source is None:
                dropped.append(f"event {event.name}: source quote is not an exact passage of block {event.source.block_id}")
                continue
            citations[f"event:{event.name}"] = event_source
            # A field cited with a wrong quotation is treated as documented by its event,
            # so an unverifiable citation never turns into a missing-input finding.
            fields = tuple(item if item.source is None or resolve(item.source) else
                           item.model_copy(update={"source": event.source}) for item in event.fields)
            events.append(event.model_copy(update={"fields": fields}))
        kept = {}
        for name in ("updates", "obligations", "invariants"):
            rules = []
            for rule in getattr(model, name):
                citation = resolve(rule.source)
                if citation is None:
                    dropped.append(f"{rule.id}: source quote is not an exact passage of block {rule.source.block_id}")
                    continue
                citations[rule.id] = citation
                rules.append(rule)
            kept[name] = tuple(rules)
        return model.model_copy(update={"events": tuple(events), **kept}), citations, dropped

    @staticmethod
    def _row(result: CaseResult) -> ExecutionRow:
        detail = " ".join(dict.fromkeys(p.message + (f" Вычислено: {p.computed}." if p.computed else "")
                                        for p in result.problems)) or STATUS_TEXT["pass"]
        return ExecutionRow(case=result.case.id, kind=result.case.kind, description=result.case.description,
                            status=result.status, detail=detail)

    def _findings(self, runs: list[tuple[CaseResult, Citation | None]], engine: Engine,
                  citations: dict[str, Citation], requirements: tuple[Requirement, ...]) -> tuple[ProposedFinding, ...]:
        acceptance = [result for result, source in runs if result.case.kind == "acceptance"]
        # If the model reproduces none of several worked examples, its translation is suspect.
        trusted = len(acceptance) < 2 or any(result.status == "pass" for result in acceptance)
        if not trusted:
            LOG.warning("Executable model reproduces none of the acceptance cases; mismatches are not reported.")
        findings: list[ProposedFinding] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for result, source in runs:
            for problem in result.problems:
                signature = (problem.kind, problem.rule_ids)
                if signature in seen:
                    continue
                finding = None
                if problem.kind == "mismatch" and trusted and source is not None:
                    finding = self._mismatch(result.case, problem, source, citations)
                elif problem.kind == "conflict":
                    finding = self._conflict(result.case, problem, engine, citations, requirements)
                elif problem.kind == "missing_input":
                    finding = self._missing(result.case, problem, engine, citations)
                if finding is not None:
                    seen.add(signature)
                    findings.append(finding)
        return tuple(findings)

    @staticmethod
    def _steps(case: TestCase) -> str:
        return "; ".join(f"{event.type}(" + ", ".join(f"{a.name}={a.value}" for a in event.fields) + ")"
                         for event in case.events)

    def _mismatch(self, case: TestCase, problem: Problem, source: Citation,
                  citations: dict[str, Citation]) -> ProposedFinding | None:
        rules = [citations[rule] for rule in problem.rule_ids if rule in citations]
        rules = [c for c in rules if c.location != source.location]
        if not rules or problem.expectation is None:
            return None
        computed = problem.computed or "иной результат"
        return ProposedFinding(
            criterion=C.CONSISTENCY,
            subject=f"Пример приёмки не следует из правил: {case.description}",
            explanation=(f"Исполняемая проверка применила правила к входным данным примера ({self._steps(case)}). "
                         f"Документ ожидает: {problem.expectation.description}. По правилам получено: {computed}."),
            recommendation="Согласовать пример приёмки и правило: исправить ожидаемый результат или уточнить правило.",
            evidence=(_evidence(source), *(_evidence(c) for c in dict.fromkeys(rules))),
            scenario=Scenario(relationship="contradiction", description=case.description,
                              expected_a=f"Пример документа: {problem.expectation.description}",
                              expected_b=f"По правилам: {computed}", evidence_a=0, evidence_b=1),
        )

    @staticmethod
    def _group(citation: Citation, requirements: tuple[Requirement, ...]) -> str | None:
        return next((r.group_id for r in requirements if contains(r.group_source.location, citation.location)), None)

    def _conflict(self, case: TestCase, problem: Problem, engine: Engine, citations: dict[str, Citation],
                  requirements: tuple[Requirement, ...]) -> ProposedFinding | None:
        invariant_id, *causes = problem.rule_ids
        limit = citations.get(invariant_id)
        if limit is None:
            return None
        group = self._group(limit, requirements)
        # One requirement qualifying its own output is not a conflict: pick a cause from another one.
        cause = next((rule for rule in causes if rule in citations
                      and citations[rule].location != limit.location
                      and (group is None or self._group(citations[rule], requirements) != group)), None)
        if cause is None:
            return None
        required = citations[cause]
        emitted = next((o for o in engine.obligations if o.id in causes), None)
        output = emitted.output if emitted is not None else "изменение состояния"
        description = f"{case.description}. Шаги: {self._steps(case)}"
        return ProposedFinding(
            criterion=C.CONSISTENCY,
            subject=f"Правила требуют несовместимого результата: {case.description}",
            explanation=(f"Исполняемая проверка выполнила сценарий ({self._steps(case)}). На шаге {problem.step} "
                         f"правило «{required.quote}» требует результат «{output}», а ограничение «{limit.quote}» "
                         "этот результат запрещает."),
            recommendation="Определить приоритет между правилами и записать поведение для этого сценария.",
            evidence=(_evidence(required), _evidence(limit)),
            scenario=Scenario(relationship="contradiction", description=description,
                              expected_a=f"Выполнить «{output}» по правилу {cause}",
                              expected_b=f"Не выполнять: ограничение {invariant_id}", evidence_a=0, evidence_b=1),
        )

    @staticmethod
    def _missing(case: TestCase, problem: Problem, engine: Engine,
                 citations: dict[str, Citation]) -> ProposedFinding | None:
        rule_id = problem.rule_ids[0]
        rule = engine.rules.get(rule_id)
        needed = citations.get(rule_id)
        event_name = getattr(rule, "on", None)
        contract = citations.get(f"event:{event_name}")
        if needed is None:
            return None
        evidence = [_evidence(needed)]
        if contract is not None and contract.location != needed.location:
            evidence.append(_evidence(contract))
        return ProposedFinding(
            criterion=C.COMPLETENESS,
            subject=f"Не задан источник данных: {problem.field}",
            explanation=(f"Чтобы выполнить правило «{needed.quote}», нужно значение {problem.field} события "
                         f"{event_name}. Ни один документированный источник это значение не передаёт, поэтому "
                         "результат правила нельзя вычислить и проверить."),
            recommendation=f"Указать, откуда система получает {problem.field}, или изменить правило.",
            evidence=tuple(evidence),
        )
