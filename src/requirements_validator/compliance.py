"""Whole-document assessment against ISO/IEC/IEEE 29148 §5.2.6 and IEEE 830 §4.3."""

import json
from typing import Self

from pydantic import model_validator

from .models import (
    DOCUMENT_CRITERIA, DocumentEvaluation, DocumentMetric, ParsedDocument, Requirement, ReviewResult, Schema,
)
from .ports import StructuredLLM, StructuredResponseError
from .prompts import DOCUMENT_RULES
from .reviewers import Assessment, CrossRequirementReviewer, SpecificationReview


class DocumentReview(Schema):
    assessments: tuple[Assessment, ...]

    @model_validator(mode="after")
    def complete(self) -> Self:
        criteria = [item.criterion for item in self.assessments]
        if set(criteria) != set(DOCUMENT_CRITERIA) or len(criteria) != len(DOCUMENT_CRITERIA):
            raise ValueError("each document criterion must be evaluated exactly once")
        return self

    def proposals(self) -> SpecificationReview:
        return SpecificationReview(findings=tuple(f for item in self.assessments for f in item.findings))


class DocumentComplianceReviewer:
    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm

    def review(self, document: ParsedDocument, requirements: tuple[Requirement, ...],
               metrics: tuple[DocumentMetric, ...]) -> DocumentReview:
        payload = json.dumps({
            **CrossRequirementReviewer._payload(document, requirements),
            "criteria": list(DOCUMENT_CRITERIA),
            "measured_facts": [metric.model_dump() for metric in metrics],
        }, ensure_ascii=False)
        try:
            return self.llm.generate(DOCUMENT_RULES, payload, DocumentReview)
        except StructuredResponseError:
            retry = (f"{DOCUMENT_RULES}\nReturn exactly {len(DOCUMENT_CRITERIA)} assessments in this order: "
                     f"{', '.join(DOCUMENT_CRITERIA)}. Supply findings only for fail.")
            return self.llm.generate(retry, payload, DocumentReview)


def document_evaluations(review: DocumentReview, result: ReviewResult) -> tuple[DocumentEvaluation, ...]:
    """Link each document-level assessment to the confirmed whole-specification findings."""
    by_criterion = {item.criterion: item for item in review.assessments}
    evaluations = []
    for criterion in DOCUMENT_CRITERIA:
        assessment = by_criterion[criterion]
        ids = tuple(sorted(f.id for f in result.findings
                           if "specification" in f.scopes and criterion in (f.criterion, *f.related_criteria)))
        outcome, rationale = assessment.outcome, assessment.rationale
        if ids and outcome != "fail":
            outcome, rationale = "fail", "См. подтверждённые замечания по спецификации в целом."
        elif not ids and outcome == "fail":
            outcome, rationale = "not_assessable", "Предложенные нарушения не подтверждены."
        evaluations.append(DocumentEvaluation(criterion=criterion, outcome=outcome, rationale=rationale,
                                              finding_ids=ids))
    return tuple(evaluations)
