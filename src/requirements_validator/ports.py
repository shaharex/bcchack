from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from .models import AnalysisRunResult, ClarificationQuestion, Finding, ParsedDocument, SeverityDecision

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model call failed or did not produce a complete response."""


class StructuredResponseError(LLMError):
    """The model output was incomplete or did not match its schema."""


class StructuredLLM(Protocol):
    @property
    def model(self) -> str: ...

    def generate(self, instruction: str, payload: str, schema: type[T]) -> T: ...


class DocumentParser(Protocol):
    def parse(self, path: Path) -> ParsedDocument: ...


class SeverityClassifier(Protocol):
    def classify(self, finding: Finding) -> SeverityDecision: ...


class ClarificationGenerator(Protocol):
    def generate(self, review: "AnalysisRunResult") -> tuple[ClarificationQuestion, ...]: ...


class ReportGenerator(Protocol):
    def write(self, result: AnalysisRunResult, destination: Path) -> None: ...
