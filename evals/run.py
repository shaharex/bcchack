"""CLI for offline corpus checks and opt-in live semantic evaluation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

# Permit ``python evals/run.py`` from a source checkout without changing pytest config.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from requirements_validator.aggregation import FindingsAggregator  # noqa: E402
from requirements_validator.gemini import GeminiClient, GeminiSettings, LLMError  # noqa: E402
from requirements_validator.models import Anchor, ParsedDocument, QualityCriterion, ReviewResult, SourceBlock  # noqa: E402
from requirements_validator.reviewers import CrossRequirementReviewer, ProposedFinding, SpecificationReview  # noqa: E402
from requirements_validator.source import EvidenceError, SourceIndex  # noqa: E402

from evals.cases import CASES, EvalCase, ExpectedIssue  # noqa: E402


@dataclass(frozen=True)
class ObservedFinding:
    criterion: QualityCriterion
    passages: frozenset[str]
    subject: str
    explanation: str
    recommendation: str
    evidence: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MaterializedCase:
    document: ParsedDocument
    fixture_id_by_block: dict[str, str]


def document_for(case: EvalCase) -> MaterializedCase:
    """Hide oracle metadata behind stable, meaningless model-visible IDs."""
    ordinal = CASES.index(case) + 1
    fixture_id_by_block = {f"b{i:03d}": passage.id for i, passage in enumerate(case.passages, start=1)}
    document = ParsedDocument(
        id=f"d{ordinal:03d}", title="Спецификация требований",
        blocks=tuple(
            SourceBlock(id=block_id, text=passage.text, section=(passage.section,))
            for block_id, passage in zip(fixture_id_by_block, case.passages, strict=True)
        ),
    )
    return MaterializedCase(document, fixture_id_by_block)


def validate_corpus() -> None:
    ids = [case.id for case in CASES]
    if len(ids) != len(set(ids)):
        raise ValueError("eval case IDs must be unique")
    for case in CASES:
        passage_ids = [p.id for p in case.passages]
        if len(passage_ids) != len(set(passage_ids)):
            raise ValueError(f"{case.id}: passage IDs must be unique")
        known = set(passage_ids)
        for issue in case.expected:
            if not issue.acceptable_passage_sets or any(not evidence or not evidence <= known for evidence in issue.acceptable_passage_sets):
                raise ValueError(f"{case.id}: invalid expected passage set")
        # Also exercise the same exact-quote resolver used by production grounding.
        materialized = document_for(case)
        visible_metadata = " ".join((materialized.document.id, materialized.document.title,
                                     *(block.id for block in materialized.document.blocks)))
        if case.id in visible_metadata or case.description in visible_metadata or any(
            passage.id in visible_metadata for passage in case.passages
        ):
            raise ValueError(f"{case.id}: oracle metadata leaked into model-visible identifiers")
        index = SourceIndex(materialized.document)
        for block_id, passage in zip(materialized.fixture_id_by_block, case.passages, strict=True):
            index.resolve(Anchor(block_id=block_id, quote=passage.text))


def validate_scorer() -> None:
    """Keep offline checks honest without substituting canned answers for a model."""
    case = CASES[0]
    expected = case.expected[0]
    evidence = tuple((passage, "test") for passage in expected.acceptable_passage_sets[0])
    good = ObservedFinding(expected.criterion, expected.acceptable_passage_sets[0], "test", "test", "test", evidence)
    wrong_criterion = ObservedFinding(QualityCriterion.COMPLETENESS, good.passages, "test", "test", "test", evidence)
    incomplete = ObservedFinding(
        expected.criterion, frozenset({next(iter(good.passages))}), "test", "test", "test", evidence[:1],
    )
    if not _matches(expected, good) or _matches(expected, wrong_criterion) or _matches(expected, incomplete):
        raise AssertionError("semantic eval scorer self-check failed")


def validate_candidate_metrics() -> None:
    case = CASES[0]
    materialized = document_for(case)
    evidence = tuple(
        {"block_id": block.id, "quote": block.text, "requirement_id": None}
        for block in materialized.document.blocks
    )
    proposals = tuple(
        ProposedFinding.model_validate({
            "criterion": "consistency", "subject": subject,
            "explanation": "Два правила задают разные результаты.",
            "recommendation": "Уточнить приоритет правил.", "evidence": evidence,
        })
        for subject in ("Первый вариант", "Перефразированный вариант")
    )
    metrics = _candidate_metrics(materialized, proposals)
    if metrics != {
        "raw_candidate_count": 2, "grounded_candidate_count": 2,
        "ungrounded_candidate_count": 0, "overlapping_evidence_candidate_count": 1,
    }:
        raise AssertionError("candidate metric self-check failed")


def _candidate_metrics(materialized: MaterializedCase, proposals: tuple[ProposedFinding, ...]) -> dict[str, int]:
    index = SourceIndex(materialized.document)
    signatures = []
    for proposal in proposals:
        try:
            blocks = frozenset(index.resolve(evidence).location.block_id for evidence in proposal.evidence)
        except EvidenceError:
            continue
        signatures.append((proposal.criterion, blocks))
    return {
        "raw_candidate_count": len(proposals),
        "grounded_candidate_count": len(signatures),
        "ungrounded_candidate_count": len(proposals) - len(signatures),
        # This is only an overlap signal: distinct claims can cite the same passages.
        "overlapping_evidence_candidate_count": len(signatures) - len(set(signatures)),
    }


def _observe(materialized: MaterializedCase, review: ReviewResult) -> tuple[list[ObservedFinding], list[dict[str, object]]]:
    fixture_ids = materialized.fixture_id_by_block
    observed = [
        ObservedFinding(
            finding.criterion,
            frozenset(fixture_ids[e.location.block_id] for e in finding.evidence),
            finding.subject,
            finding.explanation,
            finding.recommendation,
            tuple((fixture_ids[e.location.block_id], e.quote) for e in finding.evidence),
        )
        for finding in review.findings
    ]
    rejected = []
    for item in review.rejected:
        passages = set()
        for evidence in item.proposed_evidence:
            try:
                block_id = SourceIndex(materialized.document).resolve(evidence).location.block_id
            except EvidenceError:
                continue
            passages.add(fixture_ids[block_id])
        rejected.append({
            "criterion": item.criterion.value, "subject": item.subject,
            "reason": item.reason, "passages": sorted(passages),
        })
    return observed, rejected


def _matches(expected: ExpectedIssue, finding: ObservedFinding) -> bool:
    return expected.criterion == finding.criterion and any(
        evidence <= finding.passages for evidence in expected.acceptable_passage_sets
    )


def _finding_json(finding: ObservedFinding) -> dict[str, object]:
    return {
        "criterion": finding.criterion.value,
        "subject": finding.subject,
        "explanation": finding.explanation,
        "recommendation": finding.recommendation,
        "passages": sorted(finding.passages),
        "evidence": [{"passage": passage, "quote": quote} for passage, quote in finding.evidence],
    }


def score(case: EvalCase, observed: list[ObservedFinding], rejected: list[dict[str, object]],
          candidate_metrics: dict[str, int]) -> dict[str, object]:
    unmatched = set(range(len(observed)))
    issue_rows = []
    for expected in case.expected:
        match = next((i for i in sorted(unmatched) if _matches(expected, observed[i])), None)
        if match is not None:
            unmatched.remove(match)
        issue_rows.append({
            "criterion": expected.criterion.value,
            "detected_with_expected_passages": match is not None,
            "manual_review": expected.manual_review,
            "matched_finding": None if match is None else _finding_json(observed[match]),
        })
    # A broad and a focused prompt may word the same oracle-backed issue
    # differently. That is duplication, not a new false positive.
    unmatched = {i for i in unmatched if not any(_matches(expected, observed[i]) for expected in case.expected)}
    return {
        "case": case.id,
        "candidate_metrics": candidate_metrics,
        "verified_finding_count": len(observed),
        "accepted_findings": [_finding_json(finding) for finding in observed],
        "issues": issue_rows,
        "unexpected_findings": [
            _finding_json(observed[i])
            for i in sorted(unmatched)
        ],
        "rejected_candidates": rejected,
    }


def _live_settings() -> GeminiSettings:
    # Match the application CLI: cwd/.env first, then process variables win.
    file_env = dotenv_values(Path.cwd() / ".env", encoding="utf-8-sig", interpolate=False)
    env = {key: value for key, value in file_env.items() if value is not None}
    env.update(os.environ)
    return GeminiSettings.from_env(env)


def _sanitized_provider_error(exc: LLMError) -> dict[str, str]:
    message = " ".join(str(exc).split())
    message = re.sub(r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]?\s*\S+", r"\1 [REDACTED]", message)
    message = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", message)
    return {"type": type(exc).__name__, "message": message[:500]}


def run_live(cases: tuple[EvalCase, ...]) -> tuple[dict[str, object], bool]:
    settings = _live_settings()
    results = []
    with GeminiClient(settings) as llm:
        reviewer = CrossRequirementReviewer(llm)
        aggregator = FindingsAggregator(llm)
        interrupted = None
        for case in cases:
            try:
                materialized = document_for(case)
                broad = reviewer.review(materialized.document, ())
                focused = reviewer.review_focused(materialized.document, ())
                relations = reviewer.review_relations(materialized.document, ())
                proposals = broad.findings + focused.findings + relations.findings
                metrics = _candidate_metrics(materialized, proposals)
                review = aggregator.aggregate(
                    materialized.document, (), {}, SpecificationReview(findings=proposals),
                )
                metrics["merged_candidate_count"] = len(proposals) - len(review.rejected) - len(review.findings)
                observed, rejected = _observe(materialized, review)
                results.append(score(case, observed, rejected, metrics))
            except LLMError as exc:
                interrupted = {"case": case.id, "provider_error": _sanitized_provider_error(exc)}
                break

    automated = [issue for result in results for issue in result["issues"] if not issue["manual_review"]]
    manual = [issue for result in results for issue in result["issues"] if issue["manual_review"]]
    unexpected = sum(len(result["unexpected_findings"]) for result in results)
    rejected = sum(len(result["rejected_candidates"]) for result in results)
    complete = interrupted is None
    report = {
        "kind": "live_model_semantic_eval",
        "status": "complete" if complete else "incomplete",
        "model": settings.model,
        "requested_cases": [case.id for case in cases],
        "completed_cases": [result["case"] for result in results],
        "summary": {
            "automated_expected": len(automated),
            "automated_detected": sum(bool(x["detected_with_expected_passages"]) for x in automated),
            "manual_expected": len(manual),
            "manual_candidates_retrieved": sum(bool(x["detected_with_expected_passages"]) for x in manual),
            "raw_candidates": sum(result["candidate_metrics"]["raw_candidate_count"] for result in results),
            "overlapping_evidence_candidates": sum(
                result["candidate_metrics"]["overlapping_evidence_candidate_count"] for result in results
            ),
            "merged_candidates": sum(result["candidate_metrics"]["merged_candidate_count"] for result in results),
            "unexpected_findings": unexpected,
            "rejected_candidates": rejected,
            "manual_review_pending": bool(manual),
        },
        "cases": results,
    }
    if interrupted is not None:
        report["interruption"] = interrupted
    return report, complete


def main() -> int:
    parser = argparse.ArgumentParser(description="Requirements-validator semantic regression eval")
    parser.add_argument("--mode", choices=("check", "live"), default="check")
    parser.add_argument(
        "--case", action="append", choices=tuple(case.id for case in CASES), dest="case_ids",
        help="Run only this case; repeat the option for a controlled subset.",
    )
    parser.add_argument("--output", type=Path, help="Write JSON result to this path")
    args = parser.parse_args()
    if args.case_ids and len(args.case_ids) != len(set(args.case_ids)):
        parser.error("each --case may be supplied only once")
    by_id = {case.id: case for case in CASES}
    selected = tuple(by_id[case_id] for case_id in args.case_ids) if args.case_ids else CASES
    validate_corpus()
    validate_scorer()
    validate_candidate_metrics()
    complete = True
    if args.mode == "check":
        report: dict[str, object] = {
            "kind": "offline_corpus_check", "cases": len(selected),
            "selected_cases": [case.id for case in selected],
            "expected_by_criterion": dict(Counter(
                issue.criterion.value for case in selected for issue in case.expected
            )),
            "note": "No model was called; this is not a semantic quality result.",
        }
    else:
        report, complete = run_live(selected)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())

