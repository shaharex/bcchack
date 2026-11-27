import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from requirements_validator.aggregation import Verdict, Verdicts
from requirements_validator.compliance import DocumentReview
from requirements_validator.executable_model import ExecutableModel
from requirements_validator.extractor import Extraction
from requirements_validator.models import AnalysisRunResult, ClarificationQuestion, SeverityDecision
from requirements_validator.pipeline import AnalysisPipeline
from requirements_validator.reviewers import DependencyReview, FocusedReview, IndividualReviews, LifecycleReview, RelationReviews, SpecificationReview


@pytest.mark.parametrize("bad_port", [None, "severity", "clarification"])
def test_phase_two_ports_and_result_integrity(
    document, extraction, batch_assessments, llm, bad_port, skipped_comparisons, skipped_relations,
    clean_document_review, empty_executable_model,
):
    parser, severity, clarification, report = Mock(), Mock(), Mock(), Mock()
    parser.parse.return_value = document
    calls = []

    def respond(instruction, payload, schema):
        calls.append(schema)
        if schema is IndividualReviews:
            targets = [target["id"] for target in json.loads(payload)["targets"]]
            assert targets == ["R-001", "R-002", "R-003"]
            return batch_assessments(targets[::-1])
        if schema is Extraction:
            return extraction
        if schema is DependencyReview:
            return DependencyReview(dependency_gaps=())
        if schema is FocusedReview:
            return skipped_comparisons[0]
        if schema is LifecycleReview:
            return skipped_comparisons[-1]
        if schema is RelationReviews:
            assert json.loads(payload)["comparisons"][0]["link"] == 0
            return skipped_relations
        if schema is DocumentReview:
            return clean_document_review
        if schema is ExecutableModel:
            return empty_executable_model
        if schema is Verdicts:
            return Verdicts(verdicts=(Verdict(candidate=0, supported=True, reason="Подтверждено.",
                                               impact_class="needs_decision", requires_clarification=True),))
        assert schema is SpecificationReview
        req = json.loads(payload)["requirements"][0]
        return SpecificationReview.model_validate({"findings": [{
            "criterion": "verifiability", "subject": "Срок", "explanation": "Нет границы времени.",
            "recommendation": "Определить срок.", "evidence": [
                {"block_id": "b1", "quote": "срочно", "requirement_id": req["id"]}],
        }]})

    def classify(finding):
        return SeverityDecision(finding_id="unknown" if bad_port == "severity" else finding.id,
                                level="medium", rationale="Нельзя проверить срок.")

    def clarify(result):
        assert result.severities and result.questions is None
        finding_id = "unknown" if bad_port == "clarification" else result.review.findings[0].id
        return (ClarificationQuestion(finding_ids=(finding_id,), question="Каков допустимый срок?"),)

    llm.generate.side_effect = respond
    severity.classify.side_effect = classify
    clarification.generate.side_effect = clarify
    pipeline = AnalysisPipeline(llm, agent_version="test", parser=parser, report=report,
                                severity=severity, clarification=clarification)
    dest = Path("out.md")
    if bad_port:
        with pytest.raises(ValueError, match=bad_port):
            pipeline.run(Path("input.pdf"), dest)
        report.write.assert_not_called()
        return
    result = pipeline.run(Path("input.pdf"), dest)
    report.write.assert_called_once_with(result, dest)
    assert result.questions and result.model == "mock"
    assert AnalysisRunResult.model_validate_json(result.model_dump_json()) == result
    assert calls == [
        Extraction, IndividualReviews, SpecificationReview, DependencyReview,
        *map(type, skipped_comparisons), RelationReviews, DocumentReview, ExecutableModel, Verdicts,
    ]


def test_empty_extraction_still_runs_whole_document_review(
    document, llm, skipped_comparisons, skipped_relations, clean_document_review, empty_executable_model,
):
    excluded = tuple({"block_id": block.id, "kind": "supporting_context", "reason": "Нет обязательства."}
                     for block in document.blocks)
    llm.generate.side_effect = [Extraction.model_validate({"groups": [], "excluded": excluded}),
                                SpecificationReview(findings=()), DependencyReview(dependency_gaps=()), *skipped_comparisons, skipped_relations,
                                clean_document_review, empty_executable_model]
    result = AnalysisPipeline(llm, agent_version="test").analyze(document)
    assert result.requirements == () and result.review.evaluations == ()
    assert result.questions is None and result.severities is None


@pytest.mark.parametrize("corruption", ["source", "section", "coverage", "evaluation", "severity", "clarification"])
def test_imported_run_checks_evidence_and_foreign_keys(
    document, extraction, batch_assessments, llm, corruption, skipped_comparisons, skipped_relations,
    clean_document_review, empty_executable_model,
):
    llm.generate.side_effect = [
        extraction, batch_assessments(), SpecificationReview(findings=()), DependencyReview(dependency_gaps=()),
        *skipped_comparisons, skipped_relations, clean_document_review, empty_executable_model,
    ]
    result = AnalysisPipeline(llm, agent_version="test").analyze(document)
    data = result.model_dump(mode="json")
    if corruption == "source":
        data["requirements"][0]["source"]["quote"] = "подменённая цитата"
    elif corruption == "section":
        data["requirements"][0]["section"] = ["Неизвестный раздел"]
    elif corruption == "coverage":
        data["coverage"] = data["coverage"][:-1]
    elif corruption == "evaluation":
        data["review"]["evaluations"].pop()
    elif corruption == "severity":
        data["severities"] = [{"finding_id": "F-unknown", "level": "high", "rationale": "Тест"}]
    else:
        data["questions"] = [{"finding_ids": ["F-unknown"], "question": "Уточнить?"}]
    with pytest.raises(ValidationError):
        AnalysisRunResult.model_validate_json(json.dumps(data))
