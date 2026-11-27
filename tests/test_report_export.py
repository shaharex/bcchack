import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from report_template.render import RenderError, load_report, prepare_format, render_html, statistics
from report_template.render_pdf import main as render_pdf
from requirements_validator import __main__ as cli
from requirements_validator.models import AnalysisRunResult, CriterionEvaluation, QualityCriterion, ReviewResult


SAMPLE = Path(__file__).resolve().parents[1] / "report_template" / "sample_report.json"


def require_format(format):
    try:
        prepare_format(format)
    except RenderError as exc:
        pytest.skip(str(exc))


def test_sample_renders_pdf(tmp_path):
    require_format("pdf")
    wrapped = tmp_path / "input.json"
    wrapped.write_text("```json\n" + SAMPLE.read_text(encoding="utf-8") + "```\n", encoding="utf-8-sig")
    output = tmp_path / "report.pdf"
    assert render_pdf([str(wrapped), "-o", str(output)]) == 0
    with pymupdf.open(output) as pdf:
        assert "Сервис бронирования переговорных" in pdf[0].get_text()
        assert all(f"Стр. {page.number + 1} из {len(pdf)}" in page.get_text() for page in pdf)


def test_invalid_severity_has_field_path(tmp_path, capsys):
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data["checks"][0]["findings"][0]["severity"] = "Неизвестно"
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert render_pdf([str(source)]) == 1
    error = capsys.readouterr().err
    assert "checks[0].findings[0].severity" in error
    assert "Критично, Существенно, Незначительно" in error
    assert "Traceback" not in error
    assert not source.with_suffix(".pdf").exists()


def test_statistics_include_both_scopes():
    stats = statistics(load_report(SAMPLE))
    assert (stats.requirements, stats.findings, stats.clean_requirements) == (4, 6, 2)
    assert stats.by_severity == {"Критично": 2, "Существенно": 2, "Незначительно": 2}
    assert [item[0] for item in stats.critical] == ["R-01.1", "ДН-01"]


def test_html_escapes_source_text():
    report = load_report(SAMPLE)
    report.requirements[1].quote = '<script>alert("тест")</script>'
    html = render_html(report, styled=False)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(("options", "name", "signature"), [
    ([], "report.md", b"# "),
    (["--format", "pdf"], "report.pdf", b"%PDF-"),
    (["--output", "review.docx"], "review.docx", b"PK"),
    (["--output", "review.html"], "review.html", b"<!DOCTYPE html>"),
])
def test_agent_cli_exports_without_extra_analysis(
    options, name, signature, tmp_path, monkeypatch, document, requirements,
):
    format = Path(name).suffix[1:]
    if format != "md":
        require_format(format)
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "requirements.md"
    source.write_text("# Тест\n\nТребование к сервису.\n", encoding="utf-8")
    evaluations = tuple(
        CriterionEvaluation(requirement_id=item.id, criterion=criterion, outcome="pass",
                            rationale="Проверено.", finding_ids=())
        for item in requirements for criterion in QualityCriterion
    )
    result = AnalysisRunResult(
        document=document, requirements=requirements, started_at=datetime(2026, 11, 27, tzinfo=UTC),
        model="offline", agent_version="test", severities=(), questions=(),
        review=ReviewResult(evaluations=evaluations, findings=(), rejected=()),
    )

    analyses = []

    def analyze(parsed_document):
        analyses.append(parsed_document)
        return result

    monkeypatch.setattr(cli, "dotenv_values", lambda *args, **kwargs: {})
    monkeypatch.setattr(cli.GeminiSettings, "from_env", lambda *args: SimpleNamespace(model="offline"))
    monkeypatch.setattr(cli, "GeminiClient", lambda settings: nullcontext())
    monkeypatch.setattr(cli, "AnalysisPipeline", lambda *args, **kwargs: SimpleNamespace(analyze=analyze))
    assert cli.main(["--input", str(source), "--no-resume", *options]) == 0
    assert len(analyses) == 1
    assert Path(name).read_bytes().startswith(signature)
