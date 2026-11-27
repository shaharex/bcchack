"""Mapping of standard quality properties to the validator's criteria.

ISO/IEC/IEEE 29148:2018 §5.2.5 lists the characteristics of an individual
requirement and §5.2.6 those of a set of requirements. IEEE Std 830-1998 §4.3
lists the characteristics of a good software requirements specification.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from .models import AnalysisRunResult, QualityCriterion as C

Level = Literal["requirement", "document"]
Status = Literal["pass", "fail", "not_assessable", "not_run"]

ISO_29148 = "ISO/IEC/IEEE 29148:2018"
IEEE_830 = "IEEE Std 830-1998"


@dataclass(frozen=True)
class StandardProperty:
    standard: str
    clause: str
    name: str      # Term used by the standard.
    label: str     # Russian label for the report.
    criterion: C
    level: Level


PROPERTIES = (
    StandardProperty(ISO_29148, "5.2.5", "Necessary", "Необходимость", C.NECESSITY, "requirement"),
    StandardProperty(ISO_29148, "5.2.5", "Appropriate", "Уместность уровня детализации", C.IMPLEMENTATION_CONSTRAINT, "requirement"),
    StandardProperty(ISO_29148, "5.2.5", "Unambiguous", "Однозначность", C.AMBIGUITY, "requirement"),
    StandardProperty(ISO_29148, "5.2.5", "Complete", "Полнота", C.COMPLETENESS, "requirement"),
    StandardProperty(ISO_29148, "5.2.5", "Singular", "Единичность", C.ATOMICITY, "requirement"),
    StandardProperty(ISO_29148, "5.2.5", "Feasible", "Выполнимость", C.FEASIBILITY, "requirement"),
    StandardProperty(ISO_29148, "5.2.5", "Verifiable", "Проверяемость", C.VERIFIABILITY, "requirement"),
    StandardProperty(ISO_29148, "5.2.5", "Correct", "Корректность", C.CORRECTNESS, "requirement"),
    StandardProperty(ISO_29148, "5.2.5", "Conforming", "Соответствие шаблону", C.CONFORMANCE, "requirement"),
    StandardProperty(ISO_29148, "5.2.6", "Complete", "Полнота набора", C.COMPLETENESS, "document"),
    StandardProperty(ISO_29148, "5.2.6", "Consistent", "Согласованность набора", C.CONSISTENCY, "document"),
    StandardProperty(ISO_29148, "5.2.6", "Feasible", "Выполнимость набора", C.FEASIBILITY, "document"),
    StandardProperty(ISO_29148, "5.2.6", "Comprehensible", "Понятность", C.COMPREHENSIBILITY, "document"),
    StandardProperty(ISO_29148, "5.2.6", "Able to be validated", "Возможность валидации", C.VALIDATABILITY, "document"),
    StandardProperty(IEEE_830, "4.3.1", "Correct", "Корректность", C.CORRECTNESS, "document"),
    StandardProperty(IEEE_830, "4.3.2", "Unambiguous", "Однозначность", C.AMBIGUITY, "document"),
    StandardProperty(IEEE_830, "4.3.3", "Complete", "Полнота", C.COMPLETENESS, "document"),
    StandardProperty(IEEE_830, "4.3.4", "Consistent", "Согласованность", C.CONSISTENCY, "document"),
    StandardProperty(IEEE_830, "4.3.5", "Ranked for importance and/or stability", "Ранжированность", C.RANKING, "document"),
    StandardProperty(IEEE_830, "4.3.6", "Verifiable", "Проверяемость", C.VERIFIABILITY, "document"),
    StandardProperty(IEEE_830, "4.3.7", "Modifiable", "Модифицируемость", C.MODIFIABILITY, "document"),
    StandardProperty(IEEE_830, "4.3.8", "Traceable", "Прослеживаемость", C.TRACEABILITY, "document"),
)


@dataclass(frozen=True)
class ComplianceRow:
    property: StandardProperty
    status: Status
    basis: str
    finding_ids: tuple[str, ...]


def _requirement_row(prop: StandardProperty, result: AnalysisRunResult) -> ComplianceRow:
    rows = [e for e in result.review.evaluations if e.criterion == prop.criterion]
    if not rows:
        return ComplianceRow(prop, "not_run", "Требования не извлечены.", ())
    counts = Counter(e.outcome for e in rows)
    ids = tuple(sorted({fid for e in rows for fid in e.finding_ids}))
    unresolved = counts["not_assessable"] + counts["not_checked"]
    status: Status = ("fail" if counts["fail"] else "not_assessable" if unresolved
                      else "pass" if counts["pass"] else "not_run")
    basis = (f"Требований: {len(rows)}; соответствуют — {counts['pass']}, нарушения — {counts['fail']}, "
             f"нельзя оценить — {unresolved}, неприменимо — {counts['not_applicable']}.")
    return ComplianceRow(prop, status, basis, ids)


def _document_row(prop: StandardProperty, result: AnalysisRunResult) -> ComplianceRow:
    evaluations = {e.criterion: e for e in result.review.document_evaluations or ()}
    evaluation = evaluations.get(prop.criterion)
    # The whole-specification passes also confirm conflicts and gaps for these properties.
    ids = {f.id for f in result.review.findings
           if "specification" in f.scopes and prop.criterion in (f.criterion, *f.related_criteria)}
    if evaluation is not None:
        ids.update(evaluation.finding_ids)
    if ids:
        basis = evaluation.rationale if evaluation is not None and evaluation.outcome == "fail" else (
            "Подтверждены замечания по спецификации в целом.")
        return ComplianceRow(prop, "fail", basis, tuple(sorted(ids)))
    if evaluation is None:
        return ComplianceRow(prop, "not_run", "Оценка документа в целом не выполнялась.", ())
    return ComplianceRow(prop, evaluation.outcome, evaluation.rationale, ())


def compliance_rows(result: AnalysisRunResult) -> list[ComplianceRow]:
    return [(_requirement_row if prop.level == "requirement" else _document_row)(prop, result)
            for prop in PROPERTIES]
