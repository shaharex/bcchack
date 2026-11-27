import json
import re
from datetime import UTC, datetime

from markdown_it import MarkdownIt

from requirements_validator import __main__ as cli
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


def example_result():
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
        evidence=(alpha, alpha, beta),
        scopes=("individual",),
        scenario=Scenario(
            description="The same request is sent twice.",
            expected_a="Return BETA.",
            expected_b="Return ALPHA.",
            evidence_a=2,
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
    return AnalysisRunResult(
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

def test_report_renders_decision_details_without_filler_or_duplication():
    result = example_result()
    rendered = MarkdownReportGenerator().render(result)

    headers = re.findall(r"^## (.+)$", rendered, re.M)
    assert headers == [
        "Паспорт прогона", "Резюме для принятия решения", "Карта требований", "Проверка каждого требования",
        "Проверка спецификации в целом", "Сводка", "Итог",
    ]
    assert "| B1 | API → Responses | Проанализирован |" not in rendered
    diagnostic = MarkdownReportGenerator(diagnostics=True).render(result)
    assert "| B1 | API → Responses | Проанализирован |" in diagnostic
    assert r"символы \[28, 33\)" in diagnostic
    pass_line, = [line for line in rendered.splitlines() if line.startswith("- Дефектов не выявлено")]
    assert pass_line.count("**") == 12  # Six bold criterion names on one line.
    assert "- Return BETA. (Responses · стр. 2)" in rendered
    assert "- Return ALPHA. (Responses · стр. 2)" in rendered
    assert rendered.count("\nALPHA\n") == 1
    assert "The scenario and outcomes are repeated here." not in rendered
    assert rendered.count(result.review.findings[0].impact) == 1
    assert "**Что уточнить** — Choose one response." in rendered
    assert "#### D-01 — Response conflict" in rendered and "F-1" in rendered
    assert "Проверенное основание — Notes · стр. 3." in rendered
    assert "resolves this as ALPHA" in rendered
    assert "unverified proposal" not in rendered
    assert "До реализации или приёмки нужно" not in rendered
    assert "Продуктовых требований — 0" in rendered and "Других единиц анализа — 1" in rendered
    assert set(re.findall(r"\]\(#([^)]+)\)", rendered)) == set(re.findall(r'<a id="([^"]+)"', rendered))
    parsed = MarkdownIt("commonmark", {"html": True}).enable("table").parse(rendered)
    assert sum(token.type == "table_open" for token in parsed) == 1
    assert sum(token.type == "fence" for token in parsed) == 4


def test_saved_analysis_renders_without_a_provider_and_validates_citations(tmp_path, monkeypatch):
    result = example_result()
    status_by_criterion = {
        QualityCriterion.COMPLETENESS: "not_assessable",
        QualityCriterion.VERIFIABILITY: "not_checked",
        QualityCriterion.ATOMICITY: "not_applicable",
    }
    evaluations = tuple(
        row.model_copy(update={"outcome": status_by_criterion[row.criterion], "rationale": "Причина оценки."})
        if row.criterion in status_by_criterion else row
        for row in result.review.evaluations
    )
    result = result.model_copy(update={"review": result.review.model_copy(update={"evaluations": evaluations})})
    saved, output = tmp_path / "existing.json", tmp_path / "report.md"
    saved.write_text(result.model_dump_json(), encoding="utf-8")

    def unexpected_client(*args):
        raise AssertionError("Offline render must not load provider settings")

    monkeypatch.setattr(cli, "dotenv_values", unexpected_client)
    args = ["--analysis-input", str(saved), "--output", str(output)]
    assert cli.main(args) == 0
    rendered = output.read_text(encoding="utf-8")
    assert result.started_at.isoformat() in rendered and "Дата формирования отчёта" in rendered
    assert "**Полнота** · Недостаточно оснований для вывода" in rendered
    assert "**Проверяемость** · Не проверено" in rendered
    assert "**Атомарность** · Не применимо" in rendered
    assert "Незавершённых оценок (элемент × критерий) — 2" in rendered
    broken = result.model_dump(mode="json")
    broken["review"]["findings"][0]["evidence"][0]["location"]["start"] -= 1
    saved.write_text(json.dumps(broken), encoding="utf-8")
    assert cli.main(args) == 1
    assert output.read_text(encoding="utf-8") == rendered
