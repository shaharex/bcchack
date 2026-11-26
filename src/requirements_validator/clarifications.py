import json
import re
from collections import Counter

from pydantic import field_validator

from .prompts import SOURCE_RULES
from .models import AnalysisRunResult, ClarificationQuestion, Schema, Text
from .ports import StructuredLLM, StructuredResponseError

QUESTION_BATCH_SIZE = 10


QUESTION_GUIDANCE = """
Write in Russian. Ask for the missing rule, threshold, expected outcome, or choice
between conflicting behaviors, naming the affected operation or scenario. For purely
editorial defects ask which affected clauses should be separated or retained. In
importance, explain exactly what implementation or test decision depends on the answer.
Use the verified evidence provided and avoid generic questions or invented facts."""


class QuestionText(Schema):
    question: Text
    importance: Text

    @field_validator("question", "importance")
    @classmethod
    def russian_text(cls, value: str) -> str:
        if not re.search(r"[А-Яа-яЁё]", value):
            raise ValueError("expected Russian text")
        return value


class QuestionDraft(QuestionText):
    finding_id: Text


class QuestionDrafts(Schema):
    questions: tuple[QuestionDraft, ...]


class StakeholderClarificationGenerator:
    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm

    def generate(self, review: AnalysisRunResult) -> tuple[ClarificationQuestion, ...]:
        findings = tuple(f for f in review.review.findings if f.requires_clarification)
        if not findings:
            return ()
        instruction = SOURCE_RULES + """
Write one specific stakeholder question per confirmed finding. Copy each input id
verbatim into finding_id exactly once.""" + QUESTION_GUIDANCE
        single_instruction = SOURCE_RULES + """
Write one specific stakeholder question for the supplied finding.""" + QUESTION_GUIDANCE
        questions = []
        for start in range(0, len(findings), QUESTION_BATCH_SIZE):
            batch = findings[start:start + QUESTION_BATCH_SIZE]
            payload = json.dumps([f.model_dump(mode="json") for f in batch], ensure_ascii=False)
            try:
                drafts = self.llm.generate(instruction, payload, QuestionDrafts).questions
            except StructuredResponseError:
                drafts = ()
            counts = Counter(draft.finding_id for draft in drafts)
            by_id = {draft.finding_id: draft for draft in drafts if counts[draft.finding_id] == 1}
            for finding in batch:
                draft: QuestionText | None = by_id.get(finding.id)
                if draft is None:
                    draft = self.llm.generate(single_instruction, finding.model_dump_json(), QuestionText)
                questions.append(ClarificationQuestion(
                    finding_ids=(finding.id,),
                    question=f"{draft.question.strip()}\nЗачем уточнять — {draft.importance.strip()}",
                ))
        return tuple(questions)
