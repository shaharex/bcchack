from datetime import UTC, datetime

from requirements_validator.models import (
    AnalysisRunResult,
    Citation,
    ClarificationQuestion,
    CoverageRecord,
    CriterionEvaluation,
    EvidenceReference,
    Finding,
    ParsedDocument,
    QualityCriterion,
    RejectedFinding,
    Requirement,
    ReviewResult,
    Scenario,
    SeverityDecision,
    SourceBlock,
    SourceLocation,
)
from requirements_validator.report import MarkdownReportGenerator


def test_report_renders_decision_details_without_filler_or_duplication():
    rule = "For the same request return ALPHA or BETA."
    context = "The product note resolves this as ALPHA."
    document = ParsedDocument(
        id="doc",
        title="API rules",
        blocks=(
            SourceBlock(id="B1", text=rule, section=("API", "Responses"), page=2),
            SourceBlock(id="B2", text=context, section=("Notes",), page=3),
        ),
    )

    def citation(block_id: str, source: str, quote: str) -> Citation:
        start = source.index(quote)
        return Citation(
            quote=quote,
            location=SourceLocation(block_id=block_id, start=start, end=start + len(quote)),
        )

    rule_citation = citation("B1", rule, rule)
    requirement = Requirement(
        id="R-1",
        original_id=None,
        statement="Return one response.",
        source=rule_citation,
        group_id="G-1",
        group_source=rule_citation,
        section=("API", "Responses"),
        kind="acceptance_criterion",
    )
    alpha = EvidenceReference(
        **citation("B1", rule, "ALPHA").model_dump(), requirement_id=requirement.id
    )
    beta = EvidenceReference(
        **citation("B1", rule, "BETA").model_dump(), requirement_id=requirement.id
    )
    duplicate_impact = "Clients cannot choose a stable response."
    finding = Finding(
        id="F-1",
        criterion=QualityCriterion.CONSISTENCY,
        subject="Response conflict",
        explanation="The scenario and outcomes are repeated here.",
        recommendation="Choose one response.",
        requirement_ids=(requirement.id,),
        evidence=(alpha, beta),
        scopes=("individual",),
        scenario=Scenario(
            description="The same request is sent twice.",
            expected_a="Return BETA.",
            expected_b="Return ALPHA.",
            evidence_a=1,
            evidence_b=0,
        ),
        impact=duplicate_impact,
        requires_clarification=True,
    )
    evaluations = tuple(
        CriterionEvaluation(
            requirement_id=requirement.id,
            criterion=criterion,
            outcome="fail" if criterion is QualityCriterion.CONSISTENCY else "pass",
            rationale="The outcomes conflict." if criterion is QualityCriterion.CONSISTENCY else "Satisfied.",
            finding_ids=(finding.id,) if criterion is QualityCriterion.CONSISTENCY else (),
        )
        for criterion in QualityCriterion
    )
    rejected = RejectedFinding(
        status="potential",
        scope="specification",
        criterion=QualityCriterion.COMPLETENESS,
        subject="Possible product exception",
        explanation="An exception was proposed.",
        reason="The verified note resolves it.",
        requirement_ids=(),
        proposed_quotes=("unverified proposal",),
        resolving_evidence=(citation("B2", context, "resolves this as ALPHA"),),
    )
    result = AnalysisRunResult(
        document=document,
        started_at=datetime(2026, 11, 26, 12, 0, tzinfo=UTC),
        model="test-model",
        agent_version="test-agent",
        requirements=(requirement,),
        review=ReviewResult(evaluations=evaluations, findings=(finding,), rejected=(rejected,)),
        severities=(SeverityDecision(
            finding_id=finding.id, level="high", rationale=duplicate_impact,
        ),),
        questions=(ClarificationQuestion(
            finding_ids=(finding.id,), question="Which response is authoritative?",
        ),),
        coverage=(
            CoverageRecord(block_id="B1", kind="analyzed", reason="Contains the response rule."),
            CoverageRecord(block_id="B2", kind="supporting_context", reason="Resolves the proposal."),
        ),
    )

    rendered = MarkdownReportGenerator().render(result)

    assert rendered.index("## Паспорт прогона") < rendered.index("## Резюме для принятия решения")
    assert "| B1 | API → Responses | Проанализирован |" in rendered
    pass_line, = [line for line in rendered.splitlines() if line.startswith("- Пройдено:")]
    for criterion in QualityCriterion:
        if criterion is not QualityCriterion.CONSISTENCY:
            assert "**" in pass_line
    assert pass_line.count("**") == 12  # Six bold criterion names on one line.
    assert "Ожидаемый исход A (стр. 2, B1, символы [37, 41)) — Return BETA." in rendered
    assert "Ожидаемый исход B (стр. 2, B1, символы [28, 33)) — Return ALPHA." in rendered
    assert "The scenario and outcomes are repeated here." not in rendered
    assert rendered.count(duplicate_impact) == 1
    assert "- Требуется уточнение — да\n- Рекомендация — " in rendered
    assert "### Проверенное основание: Possible product exception" in rendered
    assert "стр. 3, B2, символы [17, 39)" in rendered
    assert "resolves this as ALPHA" in rendered
    assert "unverified proposal" not in rendered
    assert "Влияние — отдельно не установлено" not in rendered
    assert "Оценки отсутствуют" not in rendered
