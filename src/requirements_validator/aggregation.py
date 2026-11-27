import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from pydantic import Field, StrictBool, model_validator

from .prompts import EQUIVALENCE_RULES, VERIFY_RULES
from .models import (
    Anchor, Citation, CriterionEvaluation, Disposition, EvidenceReference, Finding, ImpactClass, ParsedDocument, QualityCriterion,
    RejectedFinding, Requirement, ReviewResult, Schema, Scope, Text,
)
from .ports import StructuredLLM, StructuredResponseError
from .reviewers import IndividualReview, ProposedFinding, SpecificationReview
from .source import EvidenceError, SourceIndex, contains, stable_id, verify_evidence_owner

LOG = logging.getLogger(__name__)
VERIFICATION_BATCH_SIZE = 10
IMPACT_ORDER = {"blocks_behavior": 0, "needs_decision": 1, "localized": 2, "uncertain": 3, None: 4}


class Verdict(Schema):
    candidate: int = Field(ge=0)
    supported: StrictBool
    reason: Text
    impact_class: ImpactClass | None
    requires_clarification: bool
    nonconfirmed_status: Disposition | None = None
    resolving_evidence: tuple[Anchor, ...] = ()
    duplicate_of: int | None = Field(default=None, description="Earlier supported candidate index, or null.")

    @model_validator(mode="after")
    def compatible(self) -> Self:
        if self.supported != (self.impact_class is not None) or (self.supported and self.nonconfirmed_status):
            raise ValueError("an impact class is required only for supported findings")
        return self


class Verdicts(Schema):
    verdicts: tuple[Verdict, ...]


class DuplicatePairs(Schema):
    # Indices into the supplied list of pairs. Empty means retain all candidates.
    same_decision: tuple[int, ...]


def related(left: Finding, right: Finding) -> bool:
    if set(left.requirement_ids).intersection(right.requirement_ids):
        return True
    return any(a.location.block_id == b.location.block_id
               and a.location.start < b.location.end and b.location.start < a.location.end
               for a in left.evidence for b in right.evidence)


@dataclass(frozen=True)
class Candidate:
    scope: Scope
    owner: str | None
    proposal: ProposedFinding

    def rejection(self, reason: str, status: Disposition = "unsupported",
                  resolving_evidence: tuple[Citation, ...] = ()) -> RejectedFinding:
        proposal = self.proposal
        ids = {e.requirement_id for e in proposal.evidence if e.requirement_id}
        if self.owner:
            ids.add(self.owner)
        return RejectedFinding(
            scope=self.scope, criterion=proposal.criterion, subject=proposal.subject,
            explanation=proposal.explanation, reason=reason, requirement_ids=tuple(sorted(ids)),
            proposed_quotes=tuple(e.quote for e in proposal.evidence),
            proposed_evidence=proposal.evidence, status=status, resolving_evidence=resolving_evidence,
        )


def evidence_order(ref: EvidenceReference) -> tuple[str, int, int, str]:
    return ref.location.block_id, ref.location.start, ref.location.end, ref.requirement_id or ""


def ground(document_id: str, index: SourceIndex, requirements: Mapping[str, Requirement],
           candidate: Candidate) -> Finding:
    proposal = candidate.proposal
    evidence: set[EvidenceReference] = set()
    sides: list[EvidenceReference] = []
    linked: set[str] = set()
    for anchor in proposal.evidence:
        owner = anchor.requirement_id
        scope = requirements[owner].source.location if owner in requirements else None
        citation = index.resolve(anchor, within=scope)
        if candidate.scope == "specification" and owner not in requirements:
            # Models sometimes cite the source's label instead of an internal R-ID.
            # Resolve it only if the exact quotation also matches the extracted source.
            matches = [r.id for r in requirements.values()
                       if (owner is None or (r.original_id is not None and r.original_id.quote == owner))
                       and (contains(r.source.location, citation.location)
                            or contains(citation.location, r.source.location))]
            if len(matches) == 1:
                owner = matches[0]
            elif matches:
                owner = None
        # A full clause can legitimately include an inline ID and more than one atom.
        # Retain the complete quotation as context and link the exact owned subspan.
        full_reference = None
        if owner in requirements and contains(citation.location, requirements[owner].source.location):
            if citation.location != requirements[owner].source.location:
                full_reference = EvidenceReference(**citation.model_dump(), requirement_id=None)
                evidence.add(full_reference)
                citation = requirements[owner].source
        ref = EvidenceReference(**citation.model_dump(), requirement_id=owner)
        verify_evidence_owner(ref, requirements)
        evidence.add(ref)
        sides.append(full_reference or ref)
        if owner is not None:
            linked.add(owner)
    if candidate.owner is not None and candidate.owner not in linked:
        raise EvidenceError(f"Отсутствует подтверждение для требования {candidate.owner}")
    refs = tuple(sorted(evidence, key=evidence_order))
    scenario = proposal.scenario
    if scenario:
        a, b = scenario.evidence_a, scenario.evidence_b
        if max(a, b) >= len(sides) or sides[a].location == sides[b].location:
            raise EvidenceError("Сценарий не привязан к двум разным исходным положениям.")
        scenario = scenario.model_copy(update={"evidence_a": refs.index(sides[a]), "evidence_b": refs.index(sides[b])})
    # A single passage can have several defects with the same short subject.
    # Paraphrases are merged only after an explicit equivalence verdict.
    claim = [" ".join(field.split()) for field in (proposal.subject, proposal.explanation)]
    finding_id = stable_id("F", document_id, proposal.criterion, claim, [e.model_dump() for e in refs])
    return Finding(id=finding_id, criterion=proposal.criterion, subject=proposal.subject,
                   explanation=proposal.explanation, recommendation=proposal.recommendation,
                   requirement_ids=tuple(sorted(linked)), evidence=refs, scopes=(candidate.scope,), scenario=scenario)


class FindingsAggregator:
    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm

    def _verify_batch(self, document: ParsedDocument, findings: list[Finding], start: int,
                      compared_in: dict[int, int]) -> dict[int, Verdict]:
        payload = {
            "document": document.model_dump(),
            "candidates": [{"candidate": i, "finding": f.model_dump()} for i, f in enumerate(findings)],
        }
        instruction = VERIFY_RULES
        payload_json = json.dumps(payload, ensure_ascii=False)
        for attempt in range(2 if len(findings) == 1 else 1):
            try:
                result = self.llm.generate(instruction, payload_json, Verdicts)
            except StructuredResponseError:
                if len(findings) > 1:
                    break
                if attempt == 0:
                    continue
                raise
            by_number = {v.candidate: v for v in result.verdicts}
            if len(result.verdicts) == len(findings) and set(by_number) == set(range(len(findings))):
                try:
                    index = SourceIndex(document)
                    for verdict in result.verdicts:
                        for anchor in verdict.resolving_evidence:
                            index.resolve(anchor)
                except EvidenceError:
                    continue
                verified = {}
                for number, verdict in by_number.items():
                    global_number = start + number
                    compared_in[global_number] = start
                    target = verdict.duplicate_of
                    if target is not None:
                        target = start + target if 0 <= target < number else -1
                    verified[global_number] = verdict.model_copy(update={
                        "candidate": global_number, "duplicate_of": target,
                    })
                return verified
        if len(findings) == 1:
            raise StructuredResponseError("evidence checker must return exactly one grounded verdict per candidate")
        LOG.warning("Gemini returned invalid verdicts for %d candidates. Retrying in smaller batches.", len(findings))
        midpoint = len(findings) // 2
        return self._verify_batch(document, findings[:midpoint], start, compared_in) | self._verify_batch(
            document, findings[midpoint:], start + midpoint, compared_in,
        )

    def _verify(self, document: ParsedDocument, findings: list[Finding]) -> dict[int, Verdict]:
        verdicts: dict[int, Verdict] = {}
        compared_in: dict[int, int] = {}
        for start in range(0, len(findings), VERIFICATION_BATCH_SIZE):
            verdicts.update(self._verify_batch(document, findings[start:start + VERIFICATION_BATCH_SIZE],
                                               start, compared_in))
        # The independent checker sees only one batch. Reconcile nearby source claims
        # it could not compare, including the same decision under different criteria.
        pairs = [(i, j) for j, right in enumerate(findings) if verdicts[j].supported
                 for i in range(j) if verdicts[i].supported and findings[i].id != right.id
                 and verdicts[j].duplicate_of != i and related(findings[i], right)
                 and (compared_in[i] != compared_in[j] or findings[i].criterion != right.criterion
                      or (len(right.evidence) >= 2 and findings[i].evidence == right.evidence))]
        for start in range(0, len(pairs), VERIFICATION_BATCH_SIZE):
            batch = pairs[start:start + VERIFICATION_BATCH_SIZE]
            numbers = sorted({i for pair in batch for i in pair})
            payload = {"pairs": batch, "findings": {i: findings[i].model_dump(include={
                "criterion", "subject", "explanation", "recommendation", "evidence"
            }) for i in numbers}}
            try:
                equivalent = self.llm.generate(EQUIVALENCE_RULES, json.dumps(payload, ensure_ascii=False),
                                               DuplicatePairs).same_decision
            except StructuredResponseError:
                LOG.warning("Could not adjudicate duplicate pairs; retaining the separate findings.")
                continue
            for position in equivalent:
                if 0 <= position < len(batch):
                    earlier, later = batch[position]
                    previous = verdicts[later].duplicate_of
                    if previous is None or not 0 <= previous <= earlier:
                        verdicts[later] = verdicts[later].model_copy(update={"duplicate_of": earlier})
        return verdicts

    def aggregate(self, document: ParsedDocument, requirements: tuple[Requirement, ...],
                  individual: Mapping[str, IndividualReview], cross: SpecificationReview) -> ReviewResult:
        index = SourceIndex(document)
        by_id = {r.id: r for r in requirements}
        if len(by_id) != len(requirements) or set(individual) != set(by_id):
            raise ValueError("individual reviews must cover every unique requirement")
        for req in requirements:
            index.verify_requirement(req)

        candidates = [
            Candidate("individual", req.id, finding) for req in requirements
            for assessment in individual[req.id].assessments for finding in assessment.findings
        ]
        candidates.extend(Candidate("specification", None, finding) for finding in cross.findings)
        grounded: list[tuple[Candidate, Finding]] = []
        rejected: list[RejectedFinding] = []
        for candidate in candidates:
            try:
                finding = ground(document.id, index, by_id, candidate)
            except EvidenceError as exc:
                rejected.append(candidate.rejection(str(exc)))
            else:
                grounded.append((candidate, finding))

        grounded.sort(key=lambda item: (item[1].criterion, item[1].requirement_ids, item[1].id))
        verdicts = self._verify(document, [f for _, f in grounded])
        accepted: dict[str, Finding] = {}
        canonical_ids: dict[str, str] = {}
        links: dict[tuple[str, QualityCriterion], set[str]] = {}
        for i, (candidate, finding) in enumerate(grounded):
            verdict = verdicts[i]
            if not verdict.supported:
                resolving = tuple(index.resolve(a) for a in verdict.resolving_evidence)
                rejected.append(candidate.rejection(verdict.reason, verdict.nonconfirmed_status or "unsupported", resolving))
                continue
            # A blocked observable outcome or a missing decision needs an answer,
            # even if the model returned an inconsistent clarification flag.
            needs_answer = verdict.requires_clarification or verdict.impact_class in {"blocks_behavior", "needs_decision"}
            finding = finding.model_copy(update={"impact": verdict.reason, "impact_class": verdict.impact_class,
                                                 "requires_clarification": needs_answer})
            affected = finding.requirement_ids
            criterion = finding.criterion
            key = canonical_ids.get(finding.id, finding.id)
            target = verdict.duplicate_of
            if target is not None and 0 <= target < i and verdicts[target].supported:
                other = grounded[target][1]
                other_key = canonical_ids[other.id]
                if (related(other, finding)
                        and canonical_ids.get(finding.id, other_key) == other_key):
                    key = other_key
                else:
                    LOG.warning("Ignored incompatible duplicate suggestion for %s.", finding.id)
            elif target is not None:
                LOG.warning("Ignored invalid duplicate target for %s.", finding.id)
            canonical_ids[finding.id] = key
            if previous := accepted.get(key):
                evidence = tuple(sorted(set(previous.evidence + finding.evidence), key=evidence_order))
                detail = finding if finding.scenario and not previous.scenario else previous
                impact = min((previous, finding), key=lambda item: IMPACT_ORDER[item.impact_class])
                scenario = detail.scenario
                if scenario:
                    scenario = scenario.model_copy(update={
                        "evidence_a": evidence.index(detail.evidence[scenario.evidence_a]),
                        "evidence_b": evidence.index(detail.evidence[scenario.evidence_b])})
                finding = detail.model_copy(update={
                    "id": key, "scopes": tuple(sorted(set(previous.scopes + finding.scopes))),
                    "evidence": evidence, "scenario": scenario,
                    "requirement_ids": tuple(sorted(set(previous.requirement_ids + finding.requirement_ids))),
                    "related_criteria": tuple(sorted({previous.criterion, finding.criterion,
                                                      *previous.related_criteria} - {detail.criterion})),
                    "requires_clarification": previous.requires_clarification or finding.requires_clarification,
                    "impact_class": impact.impact_class, "impact": impact.impact,
                })
            accepted[key] = finding
            for owner in affected:
                links.setdefault((owner, criterion), set()).add(key)

        evaluations: list[CriterionEvaluation] = []
        for req in requirements:
            for assessment in individual[req.id].assessments:
                ids = tuple(sorted(links.get((req.id, assessment.criterion), ())))
                outcome, rationale = assessment.outcome, assessment.rationale
                if ids:
                    outcome, rationale = "fail", "См. связанные подтверждённые замечания."
                elif outcome == "fail":
                    outcome, rationale = "not_assessable", "Предложенные нарушения не подтверждены."
                evaluations.append(CriterionEvaluation(requirement_id=req.id, criterion=assessment.criterion,
                                                       outcome=outcome, rationale=rationale, finding_ids=ids))
        return ReviewResult(evaluations=tuple(evaluations), findings=tuple(accepted[k] for k in sorted(accepted)),
                            rejected=tuple(rejected))
