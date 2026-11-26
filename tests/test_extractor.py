import pytest
from pydantic import ValidationError

from requirements_validator.extractor import Extraction, RequirementsExtractor
from requirements_validator.models import ParsedDocument, SourceBlock
from requirements_validator.source import Anchor, EvidenceError, SourceIndex


def test_decomposition_and_stable_source_ids(document, requirements, llm, extraction):
    a, b, c = requirements
    assert a.group_id == b.group_id != c.group_id
    assert a.original_id == b.original_id
    assert a.original_id.quote == "Р-1"
    assert a.section == ("Выдача",)
    index = SourceIndex(document)
    for r in requirements:
        index.verify(r.source)
        index.verify(r.group_source)
    data = extraction.model_dump(mode="json")
    data["groups"].reverse()
    data["groups"][1]["atoms"][0]["statement"] = "Иная краткая формулировка."
    data["groups"][0]["original_id"] = None
    llm.generate.return_value = Extraction.model_validate(data)
    updated = RequirementsExtractor(llm).extract(document)
    assert updated[-1].original_id is None
    assert [r.id for r in updated] == ["R-001", "R-002", "R-003"]
    assert [r.source.location.start for r in updated] == sorted(r.source.location.start for r in updated)
    assert {r.id for r in updated} == {r.id for r in requirements}


def test_ambiguous_or_invented_source_is_rejected(document):
    index = SourceIndex(document)
    anchor = Anchor(block_id="b1", quote="остаток")
    with pytest.raises(EvidenceError, match="Неоднозначная"):
        index.resolve(anchor)
    located = index.resolve(anchor.model_copy(update={"occurrence": 2}))
    assert located.location.start == document.blocks[0].text.rindex("остаток")
    with pytest.raises(EvidenceError):
        index.resolve(Anchor(block_id="b1", quote="ОСТАТОК"))
    with pytest.raises(EvidenceError):
        index.verify(located.model_copy(update={"quote": "остатки"}))


def test_atom_cannot_escape_group(document, llm, extraction):
    data = extraction.model_dump(mode="json")
    data["groups"][0]["atoms"][0]["source"]["quote"] = "Скрыть остаток"
    llm.generate.return_value = Extraction.model_validate(data)
    with pytest.raises(EvidenceError, match="вне"):
        RequirementsExtractor(llm).extract(document)


def test_extraction_retries_unresolvable_quotes(document, llm, extraction):
    groups = extraction.model_dump(mode="json")
    groups["groups"][0]["atoms"][0]["source"]["quote"] = "Несуществующая цитата"
    llm.generate.side_effect = [Extraction.model_validate(groups), extraction]
    requirements = RequirementsExtractor(llm).extract(document)
    assert len(requirements) == 3
    assert llm.generate.call_count == 2
    assert all(requirement.source.quote != "Несуществующая цитата" for requirement in requirements)


@pytest.mark.parametrize(("group", "label"), [(0, "Р-2"), (1, "Р-1")])
def test_real_identifier_from_another_clause_is_rejected(document, extraction, llm, group, label):
    data = extraction.model_dump(mode="json")
    data["groups"][group]["original_id"]["quote"] = label
    llm.generate.return_value = Extraction.model_validate(data)
    with pytest.raises(EvidenceError, match="Идентификатор"):
        RequirementsExtractor(llm).extract(document)


def test_separate_heading_identifier_can_apply_to_multiple_atoms(extraction, llm):
    section = ("Выдача", "Р-1")
    heading = SourceBlock(id="heading", text="### Р-1", section=section)
    body = SourceBlock(id="b1", text="Показать остаток быстро и сохранить чек.", section=section)
    doc = ParsedDocument(id="headed", title="Тест", blocks=(heading, body))
    group = extraction.groups[0].model_copy(update={
        "original_id": Anchor(block_id="heading", quote="Р-1"),
        "source": Anchor(block_id="b1", quote=body.text),
    })
    llm.generate.return_value = Extraction.model_validate({
        "groups": [group.model_dump(mode="json")],
        "excluded": [{"block_id": "heading", "kind": "metadata", "reason": "Идентификатор раздела."}],
    })
    first, second = RequirementsExtractor(llm).extract(doc)
    assert first.original_id == second.original_id
    assert first.original_id.location.block_id == "heading"
    assert first.group_id == second.group_id


def test_assessment_requires_every_criterion_once(assessments):
    complete = assessments()
    with pytest.raises(ValidationError):
        type(complete)(assessments=complete.assessments[:-1] + complete.assessments[:1])


def test_every_unrepresented_block_requires_explicit_exclusion(llm):
    document = ParsedDocument(id="one", title="Тест", blocks=(SourceBlock(id="only", text="Контекст."),))
    incomplete = Extraction(groups=())
    llm.generate.side_effect = [incomplete, incomplete]
    with pytest.raises(EvidenceError, match="Missing or inconsistent block classification"):
        RequirementsExtractor(llm).extract(document)


def test_split_retry_empty_inventory_classifies_only_requested_blocks(llm):
    doc = ParsedDocument(id="split", title="Тест", blocks=(
        SourceBlock(id="a", text="A"), SourceBlock(id="b", text="B"),
    ))
    requested = []

    def respond(_instruction, payload, _schema):
        import json

        block_ids = [block["id"] for block in json.loads(payload)["blocks"]]
        requested.append(block_ids)
        if len(block_ids) > 1:
            return Extraction(groups=())
        return Extraction.model_validate({"groups": [], "excluded": [{
            "block_id": block_ids[0], "kind": "metadata", "reason": "Нет обязательства.",
        }]})

    llm.generate.side_effect = respond
    requirements, coverage = RequirementsExtractor(llm).extract_with_coverage(doc)
    assert requirements == ()
    assert requested == [["a", "b"], ["a", "b"], ["a"], ["b"]]
    assert [item.block_id for item in coverage] == ["a", "b"]


def test_substantive_table_row_cannot_be_omitted(llm):
    text = "| Правило | Результат |\n| --- | --- |\n| A | один |\n| B | два |\n"
    doc = ParsedDocument(id="table", title="Таблица", blocks=(SourceBlock(id="t", text=text),))
    row = "| A | один |"
    incomplete = Extraction.model_validate({"groups": [{
        "source": {"block_id": "t", "quote": row}, "original_id": None,
        "atoms": [{"statement": "Для A результат один.", "source": None}],
    }]})
    llm.generate.side_effect = [incomplete, incomplete]
    with pytest.raises(EvidenceError, match="Uncovered table row.*B"):
        RequirementsExtractor(llm).extract(doc)


def test_line_anchors_ground_repeated_text_in_the_correct_original_clause(llm):
    block = SourceBlock(id="P7", text="A: print receipt\r\nB: print receipt\r\n")
    doc = ParsedDocument(id="repeat", title="Receipts", blocks=(block,))
    llm.generate.return_value = Extraction.model_validate({"groups": [{
        "source": {"block_id": block.id, "first_line": line, "last_line": line},
        "original_id": {"block_id": block.id, "quote": label},
        "atoms": [{"statement": label + " print receipt", "kind": "specification_fact",
                   "source": {"block_id": block.id, "quote": "print receipt"}}],
    } for line, label in [(2, "B:"), (1, "A:")]]})
    first, second = RequirementsExtractor(llm).extract(doc)
    assert first.source.location.start == block.text.index("print receipt")
    assert second.source.location.start == block.text.rindex("print receipt")
    assert second.group_source.quote == "B: print receipt"
    assert second.kind == "specification_fact"


def test_mixed_table_classifies_metadata_row_without_inventing_a_requirement(llm):
    text = "| Field | Value |\n| --- | --- |\n| API | Read balance |\n| Author | Example |"
    doc = ParsedDocument(id="mixed", title="Mixed table", blocks=(SourceBlock(id="t", text=text),))
    llm.generate.return_value = Extraction.model_validate({"groups": [{
        "source": {"block_id": "t", "first_line": 3, "last_line": 3}, "original_id": None,
        "atoms": [{"statement": "Read balance"}],
    }], "excluded_passages": [{
        "source": {"block_id": "t", "quote": "| Author | Example |"},
        "kind": "metadata", "reason": "Editor attribution",
    }]})
    (unit,), (entry,) = RequirementsExtractor(llm).extract_with_coverage(doc)
    assert unit.source.quote == "| API | Read balance |"
    assert entry.context_passages[0].source.quote == "| Author | Example |"
