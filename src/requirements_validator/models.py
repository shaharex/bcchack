"""Public domain and Phase 2 interchange schemas."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("expected nonblank text")
    return value  # Source quotations must retain all whitespace.


Text = Annotated[str, AfterValidator(nonblank)]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SourceBlock(Schema):
    id: Text
    text: Text
    section: tuple[Text, ...] = ()
    page: int | None = Field(default=None, ge=1)


class ParsedDocument(Schema):
    id: Text
    title: Text
    blocks: tuple[SourceBlock, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_blocks(self) -> Self:
        if len({block.id for block in self.blocks}) != len(self.blocks):
            raise ValueError("duplicate source block ID")
        return self


class SourceLocation(Schema):
    block_id: Text
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("source span must be nonempty")
        return self


class Citation(Schema):
    quote: Text
    location: SourceLocation


class Anchor(Schema):
    block_id: Text
    quote: Text
    occurrence: int | None = Field(default=None, ge=1)


class ProposedEvidence(Anchor):
    requirement_id: Text | None


UnitKind = Literal["atomic_requirement", "compound_requirement", "specification_fact", "acceptance_criterion"]
Disposition = Literal["unsupported", "potential", "improvement"]
ImpactClass = Literal["blocks_behavior", "needs_decision", "localized", "uncertain"]
Relationship = Literal["contradiction", "competing_interpretations"]


class ContextPassage(Schema):
    source: Citation
    kind: Literal["supporting_context", "metadata"]
    reason: Text


class CoverageRecord(Schema):
    block_id: Text
    kind: Literal["analyzed", "supporting_context", "metadata"]
    reason: Text
    context_passages: tuple[ContextPassage, ...] = ()


class Requirement(Schema):
    id: Text
    original_id: Citation | None
    statement: Text
    source: Citation
    group_id: Text
    group_source: Citation
    section: tuple[Text, ...]
    kind: UnitKind = "atomic_requirement"


class QualityCriterion(StrEnum):
    AMBIGUITY = "ambiguity"
    VERIFIABILITY = "verifiability"
    COMPLETENESS = "completeness"
    ATOMICITY = "atomicity"
    CONSISTENCY = "consistency"
    FEASIBILITY = "feasibility"
    IMPLEMENTATION_CONSTRAINT = "implementation_constraint"


class EvidenceReference(Citation):
    requirement_id: Text | None


Scope = Literal["individual", "specification"]
Outcome = Literal["pass", "fail", "not_assessable"]
EvaluationOutcome = Literal["pass", "fail", "not_assessable", "not_applicable", "not_checked"]


class CriterionEvaluation(Schema):
    requirement_id: Text
    criterion: QualityCriterion
    outcome: EvaluationOutcome
    rationale: Text
    finding_ids: tuple[Text, ...]

    @model_validator(mode="after")
    def confirmed_defects(self) -> Self:
        if bool(self.finding_ids) != (self.outcome == "fail"):
            raise ValueError("failed evaluations must link to confirmed findings")
        return self


class Scenario(Schema):
    relationship: Relationship = "contradiction"
    description: Text
    expected_a: Text
    expected_b: Text
    # Indices in the final evidence array, preserving which passage implies which outcome.
    evidence_a: int = Field(ge=0)
    evidence_b: int = Field(ge=0)

    @model_validator(mode="after")
    def separate_outcomes(self) -> Self:
        if (self.evidence_a == self.evidence_b or
                self.expected_a.casefold().split() == self.expected_b.casefold().split()):
            raise ValueError("scenario requires different expected outcomes and two evidence indices")
        return self


class Finding(Schema):
    id: Text
    criterion: QualityCriterion
    subject: Text
    explanation: Text
    recommendation: Text
    requirement_ids: tuple[Text, ...]
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    scopes: tuple[Scope, ...] = Field(min_length=1)
    scenario: Scenario | None = None
    impact: Text | None = None
    impact_class: ImpactClass | None = None
    requires_clarification: bool = True
    related_criteria: tuple[QualityCriterion, ...] = ()

    @model_validator(mode="after")
    def linked_requirements(self) -> Self:
        owners = {e.requirement_id for e in self.evidence if e.requirement_id is not None}
        if owners != set(self.requirement_ids) or len(owners) != len(self.requirement_ids):
            raise ValueError("finding requirement IDs must match its linked evidence")
        if self.scenario:
            a, b = self.scenario.evidence_a, self.scenario.evidence_b
            if max(a, b) >= len(self.evidence) or self.evidence[a].location == self.evidence[b].location:
                raise ValueError("scenario requires two distinct grounded evidence references")
        return self


class RejectedFinding(Schema):
    status: Disposition = "unsupported"
    scope: Scope
    criterion: QualityCriterion
    subject: Text
    explanation: Text
    reason: Text
    requirement_ids: tuple[Text, ...]
    proposed_quotes: tuple[Text, ...]
    proposed_evidence: tuple[ProposedEvidence, ...] = ()
    resolving_evidence: tuple[Citation, ...] = ()


class ReviewResult(Schema):
    evaluations: tuple[CriterionEvaluation, ...]
    findings: tuple[Finding, ...]
    rejected: tuple[RejectedFinding, ...]

    def verify_evaluations(self, requirement_ids: set[str], findings: dict[str, Finding]) -> None:
        expected = {(req_id, criterion) for req_id in requirement_ids for criterion in QualityCriterion}
        linked: set[str] = set()
        for evaluation in self.evaluations:
            row = (evaluation.requirement_id, evaluation.criterion)
            if row not in expected:
                raise ValueError("duplicate or unknown criterion evaluation")
            expected.remove(row)
            for finding_id in evaluation.finding_ids:
                finding = findings.get(finding_id)
                if (finding is None
                        or evaluation.requirement_id not in finding.requirement_ids
                        or evaluation.criterion not in (finding.criterion, *finding.related_criteria)):
                    raise ValueError("evaluation references an unrelated finding")
                linked.add(finding_id)
        if expected:
            raise ValueError("incomplete criterion evaluations")
        if not linked.issuperset(f.id for f in self.findings if "individual" in f.scopes):
            raise ValueError("individual finding has no matching criterion evaluation")


class SeverityDecision(Schema):
    finding_id: Text
    level: Literal["critical", "high", "medium", "low"]
    rationale: Text


class ClarificationQuestion(Schema):
    finding_ids: tuple[Text, ...] = Field(min_length=1)
    question: Text


class AnalysisRunResult(Schema):
    document: ParsedDocument
    started_at: AwareDatetime
    model: Text
    agent_version: Text
    requirements: tuple[Requirement, ...]
    review: ReviewResult
    severities: tuple[SeverityDecision, ...] | None
    questions: tuple[ClarificationQuestion, ...] | None
    coverage: tuple[CoverageRecord, ...] | None = None

    @model_validator(mode="after")
    def check_references(self) -> Self:
        # Source resolution also consumes these schemas.
        from .source import SourceIndex, verify_evidence_owner

        index = SourceIndex(self.document)
        requirements = {r.id: r for r in self.requirements}
        findings = {f.id: f for f in self.review.findings}
        if len(requirements) != len(self.requirements) or len(findings) != len(self.review.findings):
            raise ValueError("duplicate requirement or finding ID")
        for requirement in self.requirements:
            index.verify_requirement(requirement)
        if self.coverage is not None:
            classified = {entry.block_id for entry in self.coverage}
            analyzed = {entry.block_id for entry in self.coverage if entry.kind == "analyzed"}
            if (classified != set(index.blocks) or len(classified) != len(self.coverage)
                    or analyzed != {r.source.location.block_id for r in self.requirements}):
                raise ValueError("coverage must account for every block and match the grounded units")
            for entry in self.coverage:
                for passage in entry.context_passages:
                    index.verify(passage.source)
                    if entry.kind != "analyzed" or passage.source.location.block_id != entry.block_id:
                        raise ValueError("context passage must belong to its analyzed block")
        for finding in self.review.findings:
            for ref in finding.evidence:
                index.verify(ref)
                verify_evidence_owner(ref, requirements)
        for candidate in self.review.rejected:
            for citation in candidate.resolving_evidence:
                index.verify(citation)

        self.review.verify_evaluations(set(requirements), findings)
        if self.severities is not None and (
            len(self.severities) != len(findings) or {s.finding_id for s in self.severities} != findings.keys()
        ):
            raise ValueError("severity decisions must cover each confirmed finding exactly once")
        if self.questions is not None and any(
            finding_id not in findings for q in self.questions for finding_id in q.finding_ids
        ):
            raise ValueError("clarification references an unknown finding")
        return self
