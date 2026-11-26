import json

import pytest

from requirements_validator.aggregation import DuplicatePairs, FindingsAggregator, Verdict, Verdicts
from requirements_validator.models import QualityCriterion as Criterion
from requirements_validator.ports import LLMError, StructuredResponseError
from requirements_validator.reviewers import (
    CrossRequirementReviewer, DependencyGap, DependencyReview, IndividualReview, IndividualReviewer, IndividualReviews,
    ProposedEvidence, ProposedFinding, QuoteRepair, QuoteRepairs, SpecificationReview,
)
from requirements_validator.severity import RubricSeverityClassifier


def issue(req, criterion=Criterion.AMBIGUITY):
    return ProposedFinding(
        criterion=criterion, subject="Срок показа", explanation="Срок не определён.",
        recommendation="Уточнить предельный срок.", evidence=(ProposedEvidence(
            requirement_id=req.id, block_id="b1", quote=req.source.quote),),
    )


def verdict(i, supported=True):
    return Verdict(candidate=i, supported=supported, reason="Проверено по исходному тексту.",
                   impact_class="needs_decision" if supported else None,
                   requires_clarification=supported)


def test_individual_review_retries_an_incomplete_criterion_matrix(document, requirements, llm, assessments):
    llm.generate.reset_mock()
    expected = assessments()
    llm.generate.side_effect = [StructuredResponseError("Invalid structured response for IndividualReview"), expected]
    assert IndividualReviewer(llm).review(document, requirements[0]) == expected
    first, retry = [call.args for call in llm.generate.call_args_list]
    assert retry[1:] == first[1:]
    assert ", ".join(Criterion) in retry[0]


def test_invalid_review_batch_splits_to_recover_missing_targets(document, requirements, llm, assessments, batch_assessments):
    llm.generate.reset_mock()
    llm.generate.side_effect = [batch_assessments(("R-001", "R-001", "R-003")),
                                assessments(), batch_assessments(("R-002", "R-003"))]
    reviews = IndividualReviewer(llm).review_batch(document, requirements, requirements)
    assert reviews == {target.id: assessments() for target in requirements}
    assert [call.args[2] for call in llm.generate.call_args_list] == [
        IndividualReviews, IndividualReview, IndividualReviews,
    ]


def test_quota_failure_does_not_split_review_batch(document, requirements, llm):
    llm.generate.reset_mock()
    llm.generate.side_effect = LLMError("HTTP 429")
    with pytest.raises(LLMError, match="429"):
        IndividualReviewer(llm).review_batch(document, requirements, requirements)
    assert llm.generate.call_count == 1


def test_merge_across_passes_and_link_evaluations(document, requirements, llm, assessments):
    f = issue(requirements[0])
    reviews = {r.id: assessments() for r in requirements}
    reviews[requirements[0].id] = assessments(f)
    cross = SpecificationReview(findings=(f,))
    llm.generate.return_value = Verdicts(verdicts=(verdict(1), verdict(0)))
    result = FindingsAggregator(llm).aggregate(document, requirements, reviews, cross)
    finding, = result.findings
    assert finding.scopes == ("individual", "specification")
    assert len(result.evaluations) == 7 * len(requirements)
    failed, = [e for e in result.evaluations if e.outcome == "fail"]
    assert failed.finding_ids == (finding.id,)
    assert not result.rejected


def test_distinct_claims_on_same_quote_keep_separate_stable_ids(document, requirements, llm, assessments):
    text = issue(requirements[0])
    other = text.model_copy(update={"explanation": "Не определено начало отсчёта срока."})
    reviews = {r.id: assessments() for r in requirements}
    llm.generate.return_value = Verdicts(verdicts=(verdict(0), verdict(1)))
    aggregator = FindingsAggregator(llm)
    forward = aggregator.aggregate(document, requirements, reviews, SpecificationReview(findings=(text, other)))
    reverse = aggregator.aggregate(document, requirements, reviews, SpecificationReview(findings=(other, text)))
    assert forward.findings == reverse.findings
    assert len({finding.id for finding in forward.findings}) == 2


@pytest.mark.parametrize("problem", ["invented", "wrong_requirement", "missing_id", "no_target", "irrelevant"])
def test_unsupported_findings_are_quarantined(document, requirements, llm, assessments, problem):
    a, b, _ = requirements
    f = issue(a)
    changes = {"invented": {"quote": "Мгновенно"}, "wrong_requirement": {"requirement_id": b.id},
               "missing_id": {"requirement_id": "R-invalid"}, "no_target": {"requirement_id": None},
               "irrelevant": {}}[problem]
    f = f.model_copy(update={"evidence": (f.evidence[0].model_copy(update=changes),)})
    reviews = {r.id: assessments() for r in requirements}
    reviews[a.id] = assessments(f)
    llm.generate.reset_mock()
    llm.generate.return_value = Verdicts(verdicts=(verdict(0, False),))
    result = FindingsAggregator(llm).aggregate(document, requirements, reviews, SpecificationReview(findings=()))
    assert result.findings == ()
    assert len(result.rejected) == 1
    assert result.rejected[0].proposed_evidence == f.evidence
    assert result.evaluations[0].outcome == "not_assessable"
    assert llm.generate.call_count == int(problem == "irrelevant")


def test_cross_review_uses_entire_document_and_retains_both_sides(document, requirements, llm, assessments):
    a, _, c = requirements
    f = issue(a, Criterion.CONSISTENCY)
    refs = f.evidence + issue(c).evidence
    f = f.model_copy(update={"evidence": refs, "subject": "Видимость остатка"})
    llm.generate.return_value = SpecificationReview(findings=(f,))
    cross = CrossRequirementReviewer(llm).review(document, requirements)
    payload = json.loads(llm.generate.call_args.args[1])
    assert payload["document"] == document.model_dump(mode="json")
    assert set(payload) == {"document", "requirements"}
    assert len(payload["requirements"]) == 3
    reviews = {r.id: assessments() for r in requirements}
    agg = FindingsAggregator(llm)
    llm.generate.return_value = Verdicts(verdicts=(verdict(0),))
    result = agg.aggregate(document, requirements, reviews, cross)
    finding, = result.findings
    assert set(finding.requirement_ids) == {a.id, c.id}
    reverse = SpecificationReview(findings=(f.model_copy(update={"evidence": refs[::-1]}),))
    assert agg.aggregate(document, requirements, reviews, reverse).findings == result.findings


@pytest.mark.parametrize("verbatim", [True, False])
def test_cross_quote_repair_preserves_the_claim_and_requires_exact_source(
    document, requirements, llm, assessments, verbatim,
):
    target = requirements[0]
    original = issue(target, Criterion.COMPLETENESS)
    invalid = original.evidence[0].model_copy(update={"quote": "Invented quotation"})
    proposal = original.model_copy(update={"evidence": (invalid,)})
    repair = QuoteRepair(citation=0, quote=target.source.quote if verbatim else "Still invented")
    llm.generate.reset_mock()
    llm.generate.side_effect = [
        QuoteRepairs(repairs=(repair,)),
        Verdicts(verdicts=(verdict(0),)),
    ]

    repaired, = CrossRequirementReviewer(llm).repair_quotes(document, requirements, (proposal,))
    assert repaired == (original if verbatim else proposal)
    request = json.loads(llm.generate.call_args.args[1])
    assert set(request["blocks"]) == {invalid.block_id}
    assert request["invalid_citations"][0]["evidence"] == invalid.model_dump()
    reviews = {r.id: assessments() for r in requirements}
    result = FindingsAggregator(llm).aggregate(
        document, requirements, reviews, SpecificationReview(findings=(repaired,)),
    )
    assert len(result.findings) == int(verbatim)
    assert len(result.rejected) == int(not verbatim)
    assert llm.generate.call_count == 1 + int(verbatim)


def test_missing_dependency_keeps_targeted_consumer_and_unowned_context(
    document, requirements, llm, assessments,
):
    consumer = requirements[0]
    proposal = issue(consumer, Criterion.COMPLETENESS).model_copy(update={
        "subject": "Источник роли",
        "explanation": "Требование использует роль, но ближайшее определение не задаёт источник данных.",
        "evidence": (
            issue(consumer).evidence[0],
            ProposedEvidence(requirement_id=None, block_id="b2", quote="Клиент — владелец счёта."),
        ),
    })
    reviews = {r.id: assessments() for r in requirements}
    llm.generate.return_value = Verdicts(verdicts=(verdict(0),))
    result = FindingsAggregator(llm).aggregate(
        document, requirements, reviews, SpecificationReview(findings=(proposal,)),
    )
    finding, = result.findings
    assert finding.requirement_ids == (consumer.id,)
    assert {ref.requirement_id for ref in finding.evidence} == {consumer.id, None}


def test_cross_finding_links_previously_pass_assessments(document, requirements, llm, assessments):
    first, _, last = requirements
    proposal = issue(first, Criterion.CONSISTENCY).model_copy(update={
        "subject": "Конфликт видимости",
        "evidence": issue(first).evidence + issue(last).evidence,
    })
    reviews = {r.id: assessments() for r in requirements}
    llm.generate.return_value = Verdicts(verdicts=(verdict(0),))
    result = FindingsAggregator(llm).aggregate(
        document, requirements, reviews, SpecificationReview(findings=(proposal,)),
    )
    finding, = result.findings
    linked = [evaluation for evaluation in result.evaluations
              if evaluation.criterion is Criterion.CONSISTENCY and evaluation.requirement_id in {first.id, last.id}]
    assert len(linked) == 2
    assert all(evaluation.finding_ids == (finding.id,) for evaluation in linked)


def test_missing_verdict_cannot_pass(document, requirements, llm, assessments):
    reviews = {r.id: assessments() for r in requirements}
    llm.generate.return_value = Verdicts(verdicts=())
    with pytest.raises(StructuredResponseError, match="verdict"):
        FindingsAggregator(llm).aggregate(document, requirements, reviews,
                                          SpecificationReview(findings=(issue(requirements[0]),)))


def test_semantic_duplicate_preserves_different_quotes_and_evaluation_link(document, requirements, llm, assessments):
    original = issue(requirements[0])
    shorter = original.evidence[0].model_copy(update={"quote": "быстро"})
    paraphrase = original.model_copy(update={"subject": "Допустимое время", "evidence": (shorter,)})
    reviews = {r.id: assessments() for r in requirements}
    reviews[requirements[0].id] = assessments(original)
    llm.generate.return_value = Verdicts(verdicts=(
        verdict(0), verdict(1).model_copy(update={"duplicate_of": 0}),
    ))
    result = FindingsAggregator(llm).aggregate(document, requirements, reviews, SpecificationReview(findings=(paraphrase,)))
    finding, = result.findings
    assert finding.scopes == ("individual", "specification") and len(finding.evidence) == 2
    assert result.evaluations[0].finding_ids == (finding.id,)


@pytest.mark.parametrize("duplicate_target", [-1, 0, 1, 9])
def test_invalid_duplicate_hint_preserves_grounded_findings(
    document, requirements, llm, assessments, duplicate_target, caplog
):
    first, _, last = requirements
    reviews = {r.id: assessments() for r in requirements}
    llm.generate.return_value = Verdicts(verdicts=(
        verdict(0), Verdict(candidate=1, supported=True, reason="Подтверждено.", duplicate_of=duplicate_target,
                            impact_class="needs_decision", requires_clarification=True),
    ))
    cross = SpecificationReview(findings=(issue(first), issue(last)))
    result = FindingsAggregator(llm).aggregate(document, requirements, reviews, cross)
    assert {finding.requirement_ids for finding in result.findings} == {(first.id,), (last.id,)}
    assert "Ignored" in caplog.text


def test_invalid_verdict_batch_is_split_without_confirming_rejected_candidate(
    document, requirements, llm, assessments
):
    first, _, last = requirements
    reviews = {r.id: assessments() for r in requirements}
    llm.generate.reset_mock()
    llm.generate.side_effect = [
        StructuredResponseError("Invalid structured response for Verdicts"),
        Verdicts(verdicts=(verdict(0, False),)),
        Verdicts(verdicts=(verdict(0),)),
    ]
    cross = SpecificationReview(findings=(issue(first), issue(last)))
    result = FindingsAggregator(llm).aggregate(document, requirements, reviews, cross)
    captured = [json.loads(call.args[1]) for call in llm.generate.call_args_list]
    assert [len(payload["candidates"]) for payload in captured] == [2, 1, 1]
    assert all(payload["document"] == document.model_dump(mode="json") for payload in captured)
    rejected, = result.rejected
    accepted, = result.findings
    assert rejected.requirement_ids == tuple(captured[1]["candidates"][0]["finding"]["requirement_ids"])
    assert accepted.requirement_ids == tuple(captured[2]["candidates"][0]["finding"]["requirement_ids"])


def test_verification_batches_keep_duplicate_targets_local(monkeypatch, document, requirements, llm, assessments):
    from requirements_validator import aggregation

    monkeypatch.setattr(aggregation, "VERIFICATION_BATCH_SIZE", 2)
    groups = sorted((requirements[0], requirements[-1]), key=lambda requirement: requirement.id)
    findings = []
    for requirement in groups:
        original = issue(requirement)
        findings.extend((original, original.model_copy(update={"subject": "Допустимое время"})))
    duplicate = Verdict(candidate=1, supported=True, reason="Дубликат.", duplicate_of=0,
                        impact_class="needs_decision", requires_clarification=True)
    llm.generate.side_effect = [Verdicts(verdicts=(verdict(0), duplicate))] * 2
    reviews = {r.id: assessments() for r in requirements}
    result = FindingsAggregator(llm).aggregate(document, requirements, reviews,
                                              SpecificationReview(findings=tuple(findings)))
    assert len(result.findings) == 2
    assert {finding.requirement_ids for finding in result.findings} == {(r.id,) for r in groups}


def test_persistent_invalid_single_verdict_fails_closed(document, requirements, llm, assessments):
    reviews = {r.id: assessments() for r in requirements}
    llm.generate.reset_mock()
    llm.generate.side_effect = StructuredResponseError("Invalid structured response for Verdicts")
    with pytest.raises(StructuredResponseError, match="Verdicts"):
        FindingsAggregator(llm).aggregate(document, requirements, reviews,
                                          SpecificationReview(findings=(issue(requirements[0]),)))
    assert llm.generate.call_count == 2


def test_cross_batch_cross_criterion_duplicate_merge_preserves_both_evaluation_links(
    monkeypatch, document, requirements, llm, assessments,
):
    from requirements_validator import aggregation

    monkeypatch.setattr(aggregation, "VERIFICATION_BATCH_SIZE", 1)
    req = requirements[0]
    ambiguity = issue(req, Criterion.AMBIGUITY)
    completeness = ambiguity.model_copy(update={
        "criterion": Criterion.COMPLETENESS,
        "subject": "Граница срока",
        "explanation": "Без границы срока отсутствует завершённое условие показа.",
    })
    reviews = {r.id: assessments() for r in requirements}
    reviews[req.id] = assessments(ambiguity, completeness)

    def respond(_instruction, payload, schema):
        data = json.loads(payload)
        if schema is Verdicts:
            assert len(data["candidates"]) == 1
            return Verdicts(verdicts=(verdict(0),))
        assert schema is DuplicatePairs
        assert data["pairs"] == [[0, 1]]
        assert {row["criterion"] for row in data["findings"].values()} == {"ambiguity", "completeness"}
        return DuplicatePairs(same_decision=(0,))

    llm.generate.side_effect = respond
    result = FindingsAggregator(llm).aggregate(document, requirements, reviews, SpecificationReview(findings=()))
    finding, = result.findings
    linked = [row for row in result.evaluations
              if row.requirement_id == req.id and row.criterion in {Criterion.AMBIGUITY, Criterion.COMPLETENESS}]
    assert len(linked) == 2 and all(row.finding_ids == (finding.id,) for row in linked)
    assert {finding.criterion, *finding.related_criteria} == {Criterion.AMBIGUITY, Criterion.COMPLETENESS}


@pytest.mark.parametrize("dedicated_review", [False, True])
def test_structured_dependency_gap_becomes_grounded_end_to_end(
    document, requirements, llm, assessments, dedicated_review,
):
    consumer = requirements[0]
    gap = DependencyGap(
        subject="Источник владельца",
        consumer_needs="Экрану нужен владелец счёта.",
        available_from_contract="В контракте приведено только определение клиента.",
        unresolved_decision="Не указан источник владельца для экрана.",
        consumer=ProposedEvidence(requirement_id=consumer.id, block_id="b1", quote=consumer.source.quote),
        contract=ProposedEvidence(requirement_id=None, block_id="b2", quote="Клиент — владелец счёта."),
        recommendation="Указать источник владельца счёта.",
    )
    proposed = gap.as_finding()
    assert proposed.criterion is Criterion.COMPLETENESS
    assert proposed.evidence == (gap.consumer, gap.contract)
    assert proposed.explanation == " ".join((gap.consumer_needs, gap.available_from_contract, gap.unresolved_decision))

    reviewer = CrossRequirementReviewer(llm)
    if dedicated_review:
        llm.generate.return_value = DependencyReview(dependency_gaps=(gap,))
        cross = reviewer.review_dependencies(document, requirements)
    else:
        llm.generate.return_value = SpecificationReview(findings=(), dependency_gaps=(gap,))
        cross = reviewer.review(document, requirements)
    assert cross.findings == (proposed,)
    assert cross.dependency_gaps == ()

    reviews = {r.id: assessments() for r in requirements}
    llm.generate.return_value = Verdicts(verdicts=(verdict(0),))
    result = FindingsAggregator(llm).aggregate(document, requirements, reviews, cross)
    finding, = result.findings
    assert finding.requirement_ids == (consumer.id,)
    assert {ref.requirement_id for ref in finding.evidence} == {consumer.id, None}
    evaluation, = [row for row in result.evaluations
                   if row.requirement_id == consumer.id and row.criterion is Criterion.COMPLETENESS]
    assert evaluation.finding_ids == (finding.id,)


def test_aggregated_impact_class_drives_severity_independently_of_criterion(
    document, requirements, llm, assessments,
):
    first, _, last = requirements
    proposals = (
        issue(first, Criterion.AMBIGUITY).model_copy(update={"subject": "Локальная формулировка"}),
        issue(last, Criterion.AMBIGUITY).model_copy(update={"subject": "Блокирующий выбор"}),
    )
    reviews = {r.id: assessments() for r in requirements}

    def respond(_instruction, payload, schema):
        assert schema is Verdicts
        rows = json.loads(payload)["candidates"]
        verdicts = []
        for row in rows:
            subject = row["finding"]["subject"]
            impact = "localized" if subject == "Локальная формулировка" else "blocks_behavior"
            verdicts.append(Verdict(candidate=row["candidate"], supported=True, reason=f"Влияние: {impact}.",
                                    impact_class=impact, requires_clarification=False))
        return Verdicts(verdicts=tuple(verdicts))

    llm.generate.side_effect = respond
    result = FindingsAggregator(llm).aggregate(
        document, requirements, reviews, SpecificationReview(findings=proposals),
    )
    by_subject = {finding.subject: finding for finding in result.findings}
    rubric = RubricSeverityClassifier()
    assert rubric.classify(by_subject["Локальная формулировка"]).level == "low"
    assert rubric.classify(by_subject["Блокирующий выбор"]).level == "high"
    assert by_subject["Блокирующий выбор"].requires_clarification
    assert not by_subject["Локальная формулировка"].requires_clarification
    assert {finding.criterion for finding in result.findings} == {Criterion.AMBIGUITY}
