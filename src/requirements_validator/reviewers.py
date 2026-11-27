import json
import re
import shlex
from collections.abc import Iterable
from typing import Literal, Self

from pydantic import Field, model_validator

from .prompts import (
    CROSS_RULES, DEPENDENCY_RULES, FOCUSED_CHECKS, FOCUSED_RULES, INDIVIDUAL_RULES, LIFECYCLE_FALLBACK_RULES,
    LIFECYCLE_RULES, LINK_PLAN, QUOTE_REPAIR_RULES, RELATION_RULES,
)
from .source import EvidenceError, SourceIndex
from .models import Anchor, Outcome, ParsedDocument, ProposedEvidence, QualityCriterion, Relationship, Requirement, Scenario, Schema, Text
from .ports import StructuredLLM, StructuredResponseError


INDIVIDUAL_BATCH_SIZE = 4


class ProposedFinding(Schema):
    criterion: QualityCriterion
    subject: Text
    explanation: Text
    recommendation: Text
    evidence: tuple[ProposedEvidence, ...] = Field(min_length=1)
    scenario: Scenario | None = None


class Assessment(Schema):
    criterion: QualityCriterion
    outcome: Outcome
    rationale: Text
    findings: tuple[ProposedFinding, ...]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.outcome == "fail") != bool(self.findings):
            raise ValueError("only failed assessments must supply findings")
        if any(f.criterion != self.criterion for f in self.findings):
            raise ValueError("finding criterion differs from its assessment")
        return self


class IndividualReview(Schema):
    assessments: tuple[Assessment, ...]

    @model_validator(mode="after")
    def complete_matrix(self) -> Self:
        criteria = [item.criterion for item in self.assessments]
        if set(criteria) != set(QualityCriterion) or len(criteria) != len(QualityCriterion):
            raise ValueError("each quality criterion must be evaluated exactly once")
        return self


class TargetReview(IndividualReview):
    target_id: Text


class IndividualReviews(Schema):
    reviews: tuple[TargetReview, ...]


class DependencyGap(Schema):
    subject: Text
    consumer_needs: Text
    available_from_contract: Text
    unresolved_decision: Text
    consumer: ProposedEvidence
    contract: ProposedEvidence
    recommendation: Text

    @model_validator(mode="after")
    def two_passages(self) -> Self:
        if self.consumer.model_dump(exclude={"requirement_id"}) == self.contract.model_dump(exclude={"requirement_id"}):
            raise ValueError("a dependency gap requires both the consumer and the reviewed contract")
        return self

    def as_finding(self) -> ProposedFinding:
        return ProposedFinding(criterion=QualityCriterion.COMPLETENESS, subject=self.subject,
                               explanation=" ".join((self.consumer_needs, self.available_from_contract,
                                                     self.unresolved_decision)),
                               evidence=(self.consumer, self.contract), recommendation=self.recommendation)


class DependencyReview(Schema):
    dependency_gaps: tuple[DependencyGap, ...]


class SpecificationReview(Schema):
    findings: tuple[ProposedFinding, ...]
    dependency_gaps: tuple[DependencyGap, ...] = ()


class QuoteRepair(Schema):
    citation: int = Field(ge=0)
    quote: Text
    occurrence: int | None = Field(default=None, ge=1)


class QuoteRepairs(Schema):
    repairs: tuple[QuoteRepair, ...]


class ConflictSide(Schema):
    expected_outcome: Text
    evidence: ProposedEvidence


class SourceLink(Schema):
    block_ids: tuple[Text, ...] = Field(min_length=1)
    question: Text


class RelationPlan(Schema):
    links: tuple[SourceLink, ...]


class RelationRow(Schema):
    link: int = Field(ge=0)
    case: Text
    first: ConflictSide | None
    second: ConflictSide | None
    extra_evidence: tuple[ProposedEvidence, ...] = ()
    outcome: Literal["compatible", "unrelated", "missing_dependency", "ambiguity", "contradiction"]
    subject: Text | None
    consequence: Text | None
    correction: Text | None

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.outcome != "unrelated" and (self.first is None or self.second is None):
            raise ValueError("a comparison must give both observations")
        if self.outcome not in {"compatible", "unrelated"}:
            if not (self.subject and self.consequence and self.correction):
                raise ValueError("a defect needs a subject, consequence and correction")
            if self.first.evidence.model_dump(exclude={"requirement_id"}) == self.second.evidence.model_dump(exclude={"requirement_id"}):
                raise ValueError("a relation finding must cite two passages")
            if self.first.expected_outcome.casefold().split() == self.second.expected_outcome.casefold().split():
                raise ValueError("a relation finding must distinguish the observations")
        return self

    def as_finding(self) -> ProposedFinding | None:
        if self.outcome in {"compatible", "unrelated"}:
            return None
        criteria = {"missing_dependency": QualityCriterion.COMPLETENESS, "ambiguity": QualityCriterion.AMBIGUITY,
                    "contradiction": QualityCriterion.CONSISTENCY}
        scenario = None
        if self.outcome != "missing_dependency":
            scenario = Scenario(description=self.case, expected_a=self.first.expected_outcome,
                                expected_b=self.second.expected_outcome, evidence_a=0, evidence_b=1,
                                relationship="contradiction" if self.outcome == "contradiction" else "competing_interpretations")
        return ProposedFinding(criterion=criteria[self.outcome], subject=self.subject, scenario=scenario,
                               explanation=" ".join((self.case, self.first.expected_outcome,
                                                     self.second.expected_outcome, self.consequence)),
                               evidence=(self.first.evidence, self.second.evidence, *self.extra_evidence),
                               recommendation=self.correction)


class RelationReviews(Schema):
    rows: tuple[RelationRow, ...]


class ScenarioComparison(Schema):
    subject: Text
    scenario: Text
    first: ConflictSide
    second: ConflictSide
    context_evidence: tuple[ProposedEvidence, ...] = ()
    recommendation: Text

    @model_validator(mode="after")
    def distinct_sides(self) -> Self:
        first, second = self.first, self.second
        if " ".join(first.expected_outcome.casefold().split()) == " ".join(second.expected_outcome.casefold().split()):
            raise ValueError("a conflict must have different expected outcomes")
        if first.evidence.model_dump(exclude={"requirement_id"}) == second.evidence.model_dump(exclude={"requirement_id"}):
            raise ValueError("a conflict must cite two source passages")
        return self


class ScenarioConflict(ScenarioComparison):
    relationship: Relationship = "contradiction"

    def as_finding(self) -> ProposedFinding:
        if self.relationship == "competing_interpretations":
            first_label, second_label = "При первой трактовке", "При второй трактовке"
        else:
            first_label, second_label = "По одному правилу", "По другому правилу"
        sentences = (self.scenario.strip(), f"{first_label} — {self.first.expected_outcome.strip()}",
                     f"{second_label} — {self.second.expected_outcome.strip()}")
        explanation = " ".join(s if s.endswith((".", "!", "?", "…")) else f"{s}." for s in sentences)
        return ProposedFinding(
            criterion=(QualityCriterion.CONSISTENCY if self.relationship == "contradiction"
                       else QualityCriterion.AMBIGUITY),
            subject=self.subject,
            explanation=explanation,
            recommendation=self.recommendation,
            evidence=(self.first.evidence, self.second.evidence, *self.context_evidence),
            scenario=Scenario(relationship=self.relationship, description=self.scenario, expected_a=self.first.expected_outcome,
                              expected_b=self.second.expected_outcome, evidence_a=0, evidence_b=1),
        )


class FocusedReview(Schema):
    outcome: Outcome
    rationale: Text
    conflicts: tuple[ScenarioConflict, ...]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.outcome == "fail") != bool(self.conflicts):
            raise ValueError("only failed comparisons must supply conflicts")
        return self


class LifecycleCheck(ScenarioComparison):
    resolved_by: Anchor | None

    def as_finding(self) -> ProposedFinding:
        return ScenarioConflict(
            **self.model_dump(exclude={"resolved_by"}), relationship="competing_interpretations",
        ).as_finding()


class LifecycleReview(Schema):
    rationale: Text
    outcome: Literal["not_applicable", "resolved", "unresolved"]
    checks: tuple[LifecycleCheck, ...]

    @model_validator(mode="after")
    def complete(self) -> Self:
        outcome = "not_applicable"
        if any(check.resolved_by is None for check in self.checks):
            outcome = "unresolved"
        elif self.checks:
            outcome = "resolved"
        if self.outcome != outcome:
            raise ValueError("lifecycle outcome must match the checks and each resolving source citation")
        return self


def _validate_curl_get(review: FocusedReview) -> None:
    for conflict in review.conflicts:
        for side in (conflict.first, conflict.second):
            command = re.search(r"\bcurl(?:\.exe)?\s+([^`\r\n|]+)", side.evidence.quote)
            if command is None:
                continue
            try:
                args = shlex.split(command[1])
            except ValueError:
                continue
            if not {"-G", "--get"}.intersection(args):
                continue
            # Leave explicit overrides and examples with multiple requests to the reviewer.
            if any(arg.startswith(("-X", "--request")) or arg in {"-I", "--head", "-:", "--next"} for arg in args):
                continue
            if side.expected_outcome.strip().upper() != "GET":
                raise StructuredResponseError("A curl example with -G or --get specifies GET")


class IndividualReviewer:
    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm

    def review(self, document: ParsedDocument, requirement: Requirement,
               requirements: tuple[Requirement, ...] = ()) -> IndividualReview:
        instruction = INDIVIDUAL_RULES
        payload = {"document": document.model_dump(), "target": requirement.model_dump()}
        if requirements:
            payload["index"] = self._index(r for r in requirements if r.id != requirement.id)
        payload_json = json.dumps(payload, ensure_ascii=False)
        try:
            return self.llm.generate(instruction, payload_json, IndividualReview)
        except StructuredResponseError:
            criteria = ", ".join(QualityCriterion)
            retry = f"{instruction}\nReturn exactly seven assessments in this order: {criteria}."
            return self.llm.generate(retry, payload_json, IndividualReview)

    @staticmethod
    def _index(requirements: Iterable[Requirement]) -> list[dict[str, str]]:
        return [{"id": r.id, "block": r.source.location.block_id, "quote": r.source.quote} for r in requirements]

    def review_batch(self, document: ParsedDocument, targets: tuple[Requirement, ...],
                     requirements: tuple[Requirement, ...]) -> dict[str, IndividualReview]:
        if len(targets) == 1:
            target, = targets
            return {target.id: self.review(document, target, requirements)}
        ids = {target.id for target in targets}
        payload = {
            "document": document.model_dump(),
            "targets": [r.model_dump() for r in targets],
            "index": self._index(r for r in requirements if r.id not in ids),
        }
        instruction = INDIVIDUAL_RULES + """
Review each supplied target independently. In reviews, return one review per target,
copying its id to target_id. Include all seven criteria for every target.
Keep justifications for passing criteria brief."""
        try:
            batch = self.llm.generate(instruction, json.dumps(payload, ensure_ascii=False), IndividualReviews)
            reviews = {item.target_id: IndividualReview(assessments=item.assessments) for item in batch.reviews}
            if len(batch.reviews) != len(targets) or reviews.keys() != ids:
                raise StructuredResponseError("individual review returned missing, duplicate or unknown target IDs")
            return reviews
        except StructuredResponseError:
            midpoint = len(targets) // 2
            return (self.review_batch(document, targets[:midpoint], requirements)
                    | self.review_batch(document, targets[midpoint:], requirements))


class CrossRequirementReviewer:
    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm

    def review(self, document: ParsedDocument, requirements: tuple[Requirement, ...]) -> SpecificationReview:
        instruction = CROSS_RULES
        payload = self._payload(document, requirements)
        raw = self.llm.generate(instruction, json.dumps(payload, ensure_ascii=False), SpecificationReview)
        return SpecificationReview(findings=raw.findings + tuple(g.as_finding() for g in raw.dependency_gaps))

    def review_dependencies(self, document: ParsedDocument,
                            requirements: tuple[Requirement, ...]) -> SpecificationReview:
        payload = json.dumps(self._payload(document, requirements), ensure_ascii=False)
        try:
            review = self.llm.generate(DEPENDENCY_RULES, payload, DependencyReview)
        except StructuredResponseError:
            review = self.llm.generate(DEPENDENCY_RULES + "\nReturn a dependency_gaps array.", payload, DependencyReview)
        return SpecificationReview(findings=tuple(gap.as_finding() for gap in review.dependency_gaps))

    def review_focused(self, document: ParsedDocument, requirements: tuple[Requirement, ...]) -> SpecificationReview:
        payload = json.dumps(self._payload(document, requirements), ensure_ascii=False)
        findings = []
        for name, check in FOCUSED_CHECKS.items():
            instruction = FOCUSED_RULES + check
            for attempt in range(2):
                try:
                    review = self.llm.generate(instruction, payload, FocusedReview)
                    if name == "http_method":
                        _validate_curl_get(review)
                    break
                except StructuredResponseError:
                    if attempt:
                        raise
                    instruction += (
                        "\nReturn one outcome, a rationale, and conflicts only for fail. "
                        "Each conflict needs two distinct passages and different expected outcomes. "
                        "Recheck the expected outcomes against the exact citations."
                    )
            findings.extend(conflict.as_finding() for conflict in review.conflicts)
        findings.extend(self.review_lifecycle(document, requirements).findings)
        return SpecificationReview(findings=tuple(findings))

    def review_lifecycle(self, document: ParsedDocument, requirements: tuple[Requirement, ...]) -> SpecificationReview:
        payload = json.dumps(self._payload(document, requirements), ensure_ascii=False)
        index = SourceIndex(document)
        instruction = LIFECYCLE_RULES
        for attempt in range(2):
            try:
                review = self.llm.generate(instruction, payload, LifecycleReview)
                for check in review.checks:
                    if check.resolved_by is not None:
                        index.resolve(check.resolved_by)
                return SpecificationReview(findings=tuple(check.as_finding() for check in review.checks
                                                           if check.resolved_by is None))
            except (StructuredResponseError, EvidenceError):
                if attempt:
                    break
                instruction += (
                    "\nIf the rationale describes a trigger with two plausible stages and no decisive quote, "
                    "use outcome=unresolved and include its check with resolved_by=null. "
                    "Use outcome=resolved only with a check containing the verbatim resolving quote. "
                    "Every check needs two distinct citations."
                )
        fallback = self.llm.generate(FOCUSED_RULES + LIFECYCLE_FALLBACK_RULES, payload, FocusedReview)
        return SpecificationReview(findings=tuple(
            conflict.as_finding() for conflict in fallback.conflicts
            if conflict.relationship == "competing_interpretations"
        ))

    def review_relations(self, document: ParsedDocument, requirements: tuple[Requirement, ...]) -> SpecificationReview:
        source = self._payload(document, requirements)
        known = {block.id for block in document.blocks}
        if len(document.blocks) <= 3:
            links = (SourceLink(block_ids=tuple(b.id for b in document.blocks),
                                question="Какой результат следует из совместного применения этих положений?"),)
        else:
            payload = json.dumps(source, ensure_ascii=False)
            for attempt in range(2):
                plan = self.llm.generate(LINK_PLAN, payload, RelationPlan)
                if all(known.issuperset(link.block_ids) for link in plan.links):
                    break
                if attempt:
                    raise StructuredResponseError("comparison plan references unknown source blocks")
            links = tuple(dict.fromkeys(plan.links))
        findings = []
        for start in range(0, len(links), 4):
            batch = links[start:start + 4]
            comparisons = [{"link": i, **link.model_dump()} for i, link in enumerate(batch)]
            payload = json.dumps({**source, "comparisons": comparisons}, ensure_ascii=False)
            instruction = RELATION_RULES
            for attempt in range(2):
                try:
                    rows = self.llm.generate(instruction, payload, RelationReviews).rows
                    if sorted(row.link for row in rows) != list(range(len(batch))):
                        raise StructuredResponseError("incomplete relation comparisons")
                    for row in rows:
                        if row.outcome not in {"compatible", "unrelated"} and not {
                            row.first.evidence.block_id, row.second.evidence.block_id
                        }.intersection(batch[row.link].block_ids):
                            raise StructuredResponseError("comparison cites neither selected passage")
                    break
                except StructuredResponseError:
                    if attempt:
                        raise
                    instruction += "\nReturn every link index exactly once; ground the two observations in the selected source."
            findings.extend(f for row in rows if (f := row.as_finding()) is not None)
        return SpecificationReview(findings=tuple(findings))

    def repair_quotes(self, document: ParsedDocument, requirements: tuple[Requirement, ...],
                      findings: tuple[ProposedFinding, ...]) -> tuple[ProposedFinding, ...]:
        index = SourceIndex(document)
        spans = {r.id: r.source.location for r in requirements}
        invalid = []
        blocks = {}
        for finding_number, finding in enumerate(findings):
            for evidence_number, evidence in enumerate(finding.evidence):
                block = index.blocks.get(evidence.block_id)
                if block is None:
                    continue
                try:
                    index.resolve(evidence, within=spans.get(evidence.requirement_id))
                except EvidenceError:
                    invalid.append((finding_number, evidence_number, evidence))
                    blocks[block.id] = block.text
        if not invalid:
            return findings

        payload = {
            "blocks": blocks,
            "invalid_citations": [
                {"citation": number, "claim": findings[finding].explanation,
                 "evidence": evidence.model_dump()}
                for number, (finding, _, evidence) in enumerate(invalid)
            ],
        }
        try:
            reply = self.llm.generate(QUOTE_REPAIR_RULES, json.dumps(payload, ensure_ascii=False), QuoteRepairs)
        except StructuredResponseError:
            return findings

        by_citation = {repair.citation: repair for repair in reply.repairs}
        if len(by_citation) != len(reply.repairs):
            return findings
        updated = list(findings)
        for number, (finding_number, evidence_number, evidence) in enumerate(invalid):
            repair = by_citation.get(number)
            if repair is None:
                continue
            replacement = evidence.model_copy(update={"quote": repair.quote, "occurrence": repair.occurrence})
            try:
                index.resolve(replacement, within=spans.get(evidence.requirement_id))
            except EvidenceError:
                continue
            finding = updated[finding_number]
            citations = list(finding.evidence)
            citations[evidence_number] = replacement
            updated[finding_number] = finding.model_copy(update={"evidence": tuple(citations)})
        return tuple(updated)

    @staticmethod
    def _payload(document: ParsedDocument, requirements: tuple[Requirement, ...]) -> dict:
        # The document already contains the complete groups. Give reviewers a compact
        # index with the exact citable span, without repeating every parent paragraph.
        return {"document": document.model_dump(), "requirements": [
            r.model_dump(include={"id", "kind", "original_id", "statement", "source"}) for r in requirements
        ]}
