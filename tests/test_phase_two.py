import json
from contextlib import nullcontext

import pymupdf
import pytest

from requirements_validator import __main__ as cli
from requirements_validator.aggregation import Verdicts
from requirements_validator.compliance import DocumentReview
from requirements_validator.executable_model import ExecutableModel
from requirements_validator.clarifications import QuestionDrafts, QuestionText, StakeholderClarificationGenerator
from requirements_validator.extractor import Extraction
from requirements_validator.gemini import LLMError
from requirements_validator.models import EvidenceReference, Finding, QualityCriterion
from requirements_validator.parsers import DocumentParseError, FileDocumentParser
from requirements_validator.pipeline import AnalysisPipeline
from requirements_validator.report import MarkdownReportGenerator, quotation
from requirements_validator.reviewers import DependencyReview, FocusedReview, IndividualReviews, LifecycleReview, QuoteRepairs, RelationReviews, SpecificationReview
from requirements_validator.severity import RubricSeverityClassifier, SeverityRule
from requirements_validator.source import Anchor, EvidenceError, SourceIndex


def test_markdown_preserves_heading_paths_tables_lists_and_raw_spans(tmp_path):
    src = tmp_path / "sample.MD"
    table = "| Правило | Срок |\r\n| --- | --- |\r\n| Проверить | 3 с |\r\n"
    text = "# Сервис\r\n\r\nОперация\r\n--------\r\n\r\n" + table + "\r\n1. Р-1. Проверить.\r\n   Только для клиента.\r\n\r\n[ref]: /ref"
    src.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    doc = FileDocumentParser().parse(src)
    assert doc.title == "Сервис"
    assert next(b for b in doc.blocks if b.text == table).section == ("Сервис", "Операция")
    assert doc.blocks[-1].text == "[ref]: /ref"
    block = next(b for b in doc.blocks if "Р-1" in b.text)
    index = SourceIndex(doc)
    cite = index.resolve(Anchor(block_id=block.id, quote="Проверить.\r\n   Только для клиента."))
    lines = text.split("\r\n")
    first, last = [int(part[1:]) for part in block.id.split("-")]
    assert "\r\n".join(lines[first - 1:last]) + "\r\n" == block.text
    assert block.text[cite.location.start:cite.location.end] == cite.quote
    assert FileDocumentParser().parse(src) == doc


def make_pdf(path):
    with pymupdf.open() as pdf:
        pdf.new_page()
        page = pdf.new_page()
        font = pymupdf.Font("cjk")
        page.insert_font(fontname="cyr", fontbuffer=font.buffer)
        page.insert_text((70, 90), "Р-1 Выдать чек.", fontname="cyr")
        pdf.set_metadata({"title": "Выдача чека"})
        pdf.set_toc([[1, "Выдача", 2]])
        pdf.save(path)


def test_pdf_preserves_selectable_russian_text_page_and_outline(tmp_path):
    path = tmp_path / "spec.pdf"
    make_pdf(path)
    doc = FileDocumentParser().parse(path)
    assert doc.title == "Выдача чека"
    block, = doc.blocks
    assert (block.id, block.page, block.section) == ("P2", 2, ("Выдача",))
    index = SourceIndex(doc)
    index.verify(index.resolve(Anchor(block_id="P2", quote="Р-1 Выдать чек.")))


@pytest.mark.parametrize(("name", "content", "message"), [
    ("bad.md", b"\xff", "UTF-8"), ("empty.md", b" \n", "no text"),
    ("bad.pdf", b"not a PDF", "damaged"), ("file.txt", b"data", "Unsupported"),
])
def test_parse_errors(tmp_path, name, content, message):
    src = tmp_path / name
    src.write_bytes(content)
    with pytest.raises(DocumentParseError, match=message):
        FileDocumentParser().parse(src)


def test_image_only_or_empty_pdf_reports_ocr(tmp_path):
    path = tmp_path / "blank.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(path)
    with pytest.raises(DocumentParseError, match="OCR"):
        FileDocumentParser().parse(path)


def test_mixed_pdf_warns_about_skipped_image_page(tmp_path, caplog):
    path = tmp_path / "mixed.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((70, 90), "R-1 Print a receipt.")
        page = pdf.new_page()
        image = pymupdf.Pixmap(pymupdf.csRGB, 2, 2, b"\xff" * 12, False)
        page.insert_image(page.rect, pixmap=image)
        pdf.save(path)

    doc = FileDocumentParser().parse(path)
    assert [block.page for block in doc.blocks] == [1]
    assert "PDF page 2 contains images without selectable text" in caplog.text


def test_severity_rubric_and_override(requirements):
    a, _, b = requirements
    refs = tuple(EvidenceReference(**r.source.model_dump(), requirement_id=r.id) for r in (a, b))
    finding = Finding(id="F-1", subject="Остаток", explanation="Есть конфликт.",
                      recommendation="Уточнить правило.", criterion=QualityCriterion.CONSISTENCY,
                      requirement_ids=(a.id, b.id), evidence=refs, scopes=("specification",))
    rubric = RubricSeverityClassifier()
    assert rubric.classify(finding).level == "high"
    assert rubric.classify(finding.model_copy(update={"requirement_ids": (a.id,), "evidence": refs[:1]})).level == "medium"
    assert rubric.classify(finding.model_copy(update={"criterion": QualityCriterion.ATOMICITY})).level == "low"
    custom = RubricSeverityClassifier({QualityCriterion.CONSISTENCY: SeverityRule("low", "Тестовое правило.")})
    assert custom.classify(finding).rationale == "Тестовое правило."
    assert custom.classify(finding).level == "low"
    with pytest.raises(ValueError, match="high, medium or low"):
        SeverityRule("critical", "Тестовое правило.")


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (*cli.GEMINI_ENV.values(), "VALIDATOR_SEVERITY_OVERRIDES", "VALIDATOR_AGENT_VERSION"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.parametrize("extension", ["md", "pdf"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_cli_full_mocked_pipeline(
    cli_env, monkeypatch, capsys, llm, batch_assessments, extension, interrupted, skipped_comparisons, skipped_relations,
    clean_document_review, empty_executable_model,
):
    src = cli_env / f"input.{extension}"
    output = cli_env / "report.md"
    (cli_env / ".env").write_text("GEMINI_API_KEY=test\nVALIDATOR_AGENT_VERSION=test-tag\n"
                                  'VALIDATOR_SEVERITY_OVERRIDES=\'{"completeness":"high"}\'\n', encoding="utf-8")
    if extension == "pdf":
        make_pdf(src)
        quotes = ["Выдать", "чек."]
    else:
        src.write_text("# Выдача\n\nР-1 Выдать чек.\nР-2 Сохранить чек.\n", encoding="utf-8")
        quotes = ["Выдать чек.", "Сохранить чек."]

    calls = []
    extracted_ids = []

    def respond(_instruction, payload, schema):
        calls.append(schema)
        data = json.loads(payload)
        if schema is Extraction:
            source = data["blocks"][-1]
            return Extraction.model_validate({"groups": [{
                "source": {"block_id": source["id"], "quote": source["text"]},
                "original_id": {"block_id": source["id"], "quote": "Р-1"},
                "atoms": [{"statement": f"{quote} | текст", "source": {"block_id": source["id"], "quote": quote}}
                          for quote in quotes],
            }], "excluded": [
                {"block_id": block["id"], "kind": "metadata", "reason": "Заголовок или метаданные."}
                for block in data["blocks"] if block["id"] != source["id"]
            ]})
        if schema is IndividualReviews:
            extracted_ids.extend(target["id"] for target in data["targets"])
            return batch_assessments(extracted_ids)
        if schema is FocusedReview:
            return skipped_comparisons[0]
        if schema is LifecycleReview:
            return skipped_comparisons[-1]
        if schema is RelationReviews:
            return skipped_relations
        if schema is DependencyReview:
            return DependencyReview(dependency_gaps=())
        if schema is DocumentReview:
            return clean_document_review
        if schema is ExecutableModel:
            return empty_executable_model
        if schema is SpecificationReview:
            if interrupted and calls == [Extraction, IndividualReviews, SpecificationReview]:
                raise LLMError("HTTP 429. Gemini requested a retry after 48703s.")
            req = data["requirements"][0]
            candidate = {
                "criterion": "completeness", "subject": "Условие выдачи", "explanation": "Триггер выдачи не задан.",
                "recommendation": "Определить событие выдачи.", "evidence": [
                    {"block_id": req["source"]["location"]["block_id"], "quote": quotes[0], "requirement_id": req["id"]}],
            }
            invented = {**candidate, "subject": "Неверная цитата", "evidence": [
                {**candidate["evidence"][0], "quote": "Этого в источнике нет"}]}
            return SpecificationReview.model_validate({"findings": [candidate, invented]})
        if schema is QuoteRepairs:
            assert len(data["invalid_citations"]) == 1
            return QuoteRepairs(repairs=())
        if schema is Verdicts:
            assert len(data["candidates"]) == 1
            return schema.model_validate({"verdicts": [{
                "candidate": 0, "supported": True, "reason": "Подтверждено.",
                "impact_class": "needs_decision", "requires_clarification": True,
            }]})
        assert schema is QuestionDrafts and len(data) == 1
        return schema.model_validate({"questions": [{
            "finding_id": data[0]["id"], "question": "При каком событии выдаётся чек?",
            "importance": "От ответа зависит ожидаемый результат сценария выдачи.",
        }]})

    llm.generate.side_effect = respond
    monkeypatch.setattr(cli, "GeminiClient", lambda settings: nullcontext(llm))
    args = ["--input", str(src), "--output", str(output)]
    if interrupted:
        output.write_text("previous report", encoding="utf-8")
        assert cli.main(args) == 1
        assert "Progress saved" in capsys.readouterr().err
        assert output.read_text(encoding="utf-8") == "previous report"
        assert calls == [Extraction, IndividualReviews, SpecificationReview]
        calls.clear()
    assert cli.main(args) == 0
    rendered = output.read_text(encoding="utf-8")
    assert calls == [
        *([] if interrupted else [Extraction, IndividualReviews]), SpecificationReview, DependencyReview,
        *map(type, skipped_comparisons), RelationReviews, DocumentReview, ExecutableModel, QuoteRepairs, Verdicts, QuestionDrafts,
    ]
    assert "test-tag" in rendered and "Высокая серьёзность — 1" in rendered
    assert "Неподтверждённых замечаний — 1" in rendered
    confirmed = rendered.split("## Подтверждённые дефекты", 1)[1].split("## Вопросы заинтересованным сторонам", 1)[0]
    rejected = rendered.split("## Неподтверждённые замечания", 1)[1].split("## Сводка", 1)[0]
    assert "Неверная цитата" not in confirmed
    assert "Неверная цитата" in rejected and "Не подтверждено источником" in rejected
    assert {line[4:] for line in rendered.splitlines() if line.startswith("### R-")} == set(extracted_ids)
    assert rendered.count("**Однозначность**") == len(extracted_ids) == 2
    assert all(f"| {req_id} |" in rendered for req_id in extracted_ids)
    assert "\\| текст" in rendered
    assert "При каком событии выдаётся чек?" in rendered and "От ответа зависит" in rendered
    assert "Рекомендуется отложить" in rendered and ("стр. 2, P2" in rendered) == (extension == "pdf")


def test_cli_error_messages_and_existing_output(cli_env, monkeypatch, capsys, llm):
    src = cli_env / "input.md"
    out = cli_env / "report.md"
    args = ["--input", str(src), "--output", str(out)]
    assert cli.main(args) == 1 and "does not exist" in capsys.readouterr().err
    src.write_text("# Title", encoding="utf-8")
    assert cli.main(args) == 1 and "GEMINI_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("GEMINI_TIMEOUT_MS", "invalid")
    assert cli.main(args) == 1 and "GEMINI_TIMEOUT_MS" in capsys.readouterr().err
    monkeypatch.delenv("GEMINI_TIMEOUT_MS")
    monkeypatch.setenv("VALIDATOR_SEVERITY_OVERRIDES", '{"consistency":"critical"}')
    assert cli.main(args) == 1 and "VALIDATOR_SEVERITY_OVERRIDES" in capsys.readouterr().err
    monkeypatch.delenv("VALIDATOR_SEVERITY_OVERRIDES")
    assert cli.main(["--input", str(src), "--output", str(src)]) == 1
    assert "distinct" in capsys.readouterr().err and src.read_text() == "# Title"
    out.write_text("previous report", encoding="utf-8")
    monkeypatch.setattr(cli, "GeminiClient", lambda settings: nullcontext(llm))
    llm.generate.side_effect = LLMError("test model error")
    assert cli.main(args) == 1 and "Gemini analysis failed" in capsys.readouterr().err
    assert out.read_text() == "previous report"


def test_cli_settings_are_scoped_to_invocation(
    cli_env, monkeypatch, llm, skipped_comparisons, skipped_relations, clean_document_review,
    empty_executable_model,
):
    src = cli_env / "input.md"
    src.write_text("# Чек", encoding="utf-8")
    args = ["--input", str(src), "--output", "report.md"]
    settings_seen = []

    def client(settings):
        settings_seen.append(settings)
        return nullcontext(llm)

    monkeypatch.setattr(cli, "GeminiClient", client)
    def respond(_instruction, payload, schema):
        if schema is Extraction:
            blocks = json.loads(payload)["blocks"]
            return Extraction.model_validate({"groups": [], "excluded": [
                {"block_id": block["id"], "kind": "metadata", "reason": "Нет обязательства."}
                for block in blocks
            ]})
        if schema is DependencyReview:
            return DependencyReview(dependency_gaps=())
        if schema is SpecificationReview:
            return SpecificationReview(findings=())
        if schema is RelationReviews:
            return skipped_relations
        if schema is DocumentReview:
            return clean_document_review
        if schema is ExecutableModel:
            return empty_executable_model
        if schema is LifecycleReview:
            return skipped_comparisons[-1]
        return skipped_comparisons[0]

    llm.generate.side_effect = respond
    config = cli_env / ".env"
    config.write_text("GEMINI_API_KEY=test\nGEMINI_MODEL=file-model\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_MODEL", "process-model")
    assert cli.main(args) == 0
    monkeypatch.delenv("GEMINI_MODEL")
    config.write_text("GEMINI_API_KEY=new-test\nGEMINI_MODEL=updated-model\n", encoding="utf-8")
    assert cli.main(args) == 0
    assert [settings.model for settings in settings_seen] == ["process-model", "updated-model"]
    assert settings_seen[1].api_key.get_secret_value() == "new-test"


def test_report_fences_and_evidence_recheck(
    document, extraction, llm, batch_assessments, skipped_comparisons, skipped_relations, clean_document_review,
    empty_executable_model,
):
    llm.generate.side_effect = [
        extraction, batch_assessments(), SpecificationReview(findings=()), DependencyReview(dependency_gaps=()),
        *skipped_comparisons, skipped_relations, clean_document_review, empty_executable_model,
    ]
    run = AnalysisPipeline(llm, agent_version="fixture").analyze(document)
    writer = MarkdownReportGenerator()
    assert writer.render(run) == writer.render(run)
    assert quotation("Ссылка ```\r\nтекст\r\n") == "````\nСсылка ```\r\nтекст\r\n````"
    assert StakeholderClarificationGenerator(llm).generate(run) == ()
    first = run.requirements[0]
    invalid = first.model_copy(update={"source": first.source.model_copy(update={"quote": "подмена"})})
    with pytest.raises(EvidenceError):
        writer.render(run.model_copy(update={"requirements": (invalid,)}))
    misplaced = Finding(
        id="F-incorrect", subject="Остаток", explanation="Есть конфликт.", recommendation="Уточнить правило.",
        criterion=QualityCriterion.CONSISTENCY, requirement_ids=(run.requirements[1].id,),
        evidence=(EvidenceReference(**first.source.model_dump(), requirement_id=run.requirements[1].id),),
        scopes=("specification",),
    )
    review = run.review.model_copy(update={"findings": (misplaced,)})
    with pytest.raises(EvidenceError, match="не относится"):
        writer.render(run.model_copy(update={"review": review}))


def test_clarification_recovers_unmatched_findings(
    document, extraction, llm, batch_assessments, skipped_comparisons, skipped_relations, clean_document_review,
    empty_executable_model,
):
    llm.generate.side_effect = [
        extraction, batch_assessments(), SpecificationReview(findings=()), DependencyReview(dependency_gaps=()),
        *skipped_comparisons, skipped_relations, clean_document_review, empty_executable_model,
    ]
    run = AnalysisPipeline(llm, agent_version="fixture").analyze(document)
    req = run.requirements[0]
    finding = Finding(
        id="F-known", criterion=QualityCriterion.AMBIGUITY, subject="Срок", explanation="Срок не указан.",
        recommendation="Указать срок.", requirement_ids=(req.id,), scopes=("individual",),
        evidence=(EvidenceReference(**req.source.model_dump(), requirement_id=req.id),),
    )
    missing = finding.model_copy(update={"id": "F-missing"})
    duplicate = finding.model_copy(update={"id": "F-duplicate"})
    findings = (missing, finding, duplicate)
    run = run.model_copy(update={"review": run.review.model_copy(update={"findings": findings})})
    draft = {"question": "Какой срок допустим?", "importance": "Нужен для теста."}
    replies = QuestionDrafts.model_validate({"questions": [
        {**draft, "finding_id": finding_id}
        for finding_id in ("F-other", finding.id, duplicate.id, duplicate.id)
    ]})
    recovered = QuestionText(question="Каков максимальный срок?", importance="Нужен для проверки срока.")
    llm.generate.reset_mock()
    llm.generate.side_effect = [replies, recovered, recovered]

    questions = StakeholderClarificationGenerator(llm).generate(run)

    assert [q.finding_ids for q in questions] == [(f.id,) for f in findings]
    assert [q.question.splitlines()[0] for q in questions] == [
        recovered.question, draft["question"], recovered.question,
    ]
    calls = [call.args for call in llm.generate.call_args_list]
    assert [schema for _, _, schema in calls] == [QuestionDrafts, QuestionText, QuestionText]
    assert [json.loads(payload)["id"] for _, payload, _ in calls[1:]] == [missing.id, duplicate.id]
