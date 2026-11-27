import json
from unittest.mock import MagicMock, Mock

import pytest

from evals import run
from requirements_validator.aggregation import Verdict, Verdicts
from requirements_validator.gemini import GeminiSettings
from requirements_validator.reviewers import ProposedFinding, SpecificationReview


@pytest.mark.parametrize(
    "broad_found,dedicated_found", [(False, True), (True, True), (True, False)]
)
def test_comparison_reuses_discovery_and_removes_only_dedicated_occurrences(
    monkeypatch, broad_found, dedicated_found
):
    case = run.CASES[0]
    document = run.document_for(case).document
    finding = ProposedFinding.model_validate(
        {
            "criterion": "consistency",
            "subject": "Conflicting limits",
            "explanation": "Different limits apply.",
            "recommendation": "Clarify the limit.",
            "evidence": [
                {"block_id": b.id, "quote": b.text, "requirement_id": None}
                for b in document.blocks
            ],
        }
    )
    reviewer = Mock()
    reviewer.review.return_value = SpecificationReview(
        findings=(finding,) if broad_found else ()
    )
    reviewer.review_dependencies.return_value = SpecificationReview(
        findings=(finding,) if dedicated_found else ()
    )
    reviewer.review_focused.return_value = reviewer.review_relations.return_value = (
        SpecificationReview(findings=())
    )
    reviewer.repair_quotes.side_effect = lambda doc, requirements, proposals: proposals
    monkeypatch.setattr(run, "CrossRequirementReviewer", lambda llm: reviewer)
    monkeypatch.setattr(
        run, "_live_settings", lambda: GeminiSettings(api_key="offline")
    )
    client = MagicMock()
    monkeypatch.setattr(run, "GeminiClient", lambda settings: client)
    batch_sizes = []

    def verify(instruction, payload, schema):
        assert schema is Verdicts
        candidates = json.loads(payload)["candidates"]
        batch_sizes.append(len(candidates))
        return Verdicts(
            verdicts=tuple(
                Verdict(
                    candidate=c["candidate"],
                    supported=True,
                    reason="Conflicting limits.",
                    impact_class="needs_decision",
                    requires_clarification=True,
                )
                for c in candidates
            )
        )

    client.__enter__.return_value.generate.side_effect = verify
    report, complete = run.run_live((case,), compare_dependencies=True)
    assert complete
    row = report["cases"][0]
    comparison = row["dependency_comparison"]
    assert row["issues"][0]["detected_with_expected_passages"]
    assert (
        comparison["without_dedicated_pass"]["issues"][0][
            "detected_with_expected_passages"
        ]
        == broad_found
    )
    assert len(comparison["dedicated_candidates"]) == int(dedicated_found)
    assert batch_sizes == ([2, 1] if broad_found and dedicated_found else [1])
    reviewer.review_dependencies.assert_called_once()
    reviewer.repair_quotes.assert_called_once()
