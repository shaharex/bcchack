"""Входные данные отчёта и проверка связей между его разделами."""

from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, ValidationError, ValidationInfo,
    field_validator, model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("укажите непустую строку")
    return value


Text = Annotated[str, AfterValidator(nonblank)]
Severity = Literal["Критично", "Существенно", "Незначительно"]
ReportId = Annotated[str, Field(pattern=r"^R-[0-9]{2,}$")]
FindingId = Annotated[str, Field(pattern=r"^R-[0-9]{2,}\.[1-9][0-9]*$")]
SetFindingId = Annotated[str, Field(pattern=r"^ДН-[0-9]{2,}$")]


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Passport(ReportModel):
    document_title: Text
    source: Text
    run_date: Text
    model: Text
    agent_version: Text


class Requirement(ReportModel):
    report_id: ReportId
    doc_id: Text | None
    section: Text
    summary: Text
    quote: Text | None = Field(default=None, validate_default=True)

    @field_validator("quote")
    @classmethod
    def require_quote_without_doc_id(cls, value: str | None, info: ValidationInfo) -> str | None:
        if "doc_id" in info.data and info.data["doc_id"] is None and value is None:
            raise ValueError("укажите дословную цитату для требования без ID в документе")
        return value


class Finding(ReportModel):
    id: FindingId
    severity: Severity
    criterion: Text
    location: Text
    quote: Text
    explanation: Text
    fix: Text


class Check(ReportModel):
    requirement_id: ReportId
    findings: list[Finding]


class SetFinding(ReportModel):
    id: SetFindingId
    severity: Severity
    criterion: Text
    affected: list[ReportId]
    explanation: Text
    fix: Text


class ComplianceRow(ReportModel):
    standard: Text
    clause: Text
    property: Text
    status: Literal["Соответствует", "Не соответствует", "Нельзя оценить", "Не проверялось"]
    basis: Text
    findings: list[Text]


class Conclusion(ReportModel):
    can_start: bool
    reason: Text
    priorities: list[Text]


class Report(ReportModel):
    passport: Passport
    requirements: list[Requirement]
    checks: list[Check]
    set_findings: list[SetFinding]
    conclusion: Conclusion
    compliance: list[ComplianceRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_links(self) -> Self:
        errors: list[InitErrorDetails] = []

        def error(loc: tuple[str | int, ...], value: str, message: str) -> None:
            errors.append(InitErrorDetails(
                type=PydanticCustomError("report_reference", message), loc=loc, input=value,
            ))

        requirements: set[str] = set()
        for i, requirement in enumerate(self.requirements):
            if requirement.report_id in requirements:
                error(("requirements", i, "report_id"), requirement.report_id, "повторяющийся ID требования")
            requirements.add(requirement.report_id)

        checked: set[str] = set()
        finding_ids: set[str] = set()
        for i, check in enumerate(self.checks):
            required_id = check.requirement_id
            if required_id not in requirements:
                error(("checks", i, "requirement_id"), required_id, f"неизвестное требование {required_id}")
            elif required_id in checked:
                error(("checks", i, "requirement_id"), required_id, f"повторная проверка {required_id}")
            checked.add(required_id)
            for j, finding in enumerate(check.findings):
                loc = ("checks", i, "findings", j, "id")
                if finding.id.partition(".")[0] != required_id:
                    error(loc, finding.id, f"ID замечания должен начинаться с {required_id}.")
                if finding.id in finding_ids:
                    error(loc, finding.id, "повторяющийся ID замечания")
                finding_ids.add(finding.id)

        for i, finding in enumerate(self.set_findings):
            if finding.id in finding_ids:
                error(("set_findings", i, "id"), finding.id, "повторяющийся ID замечания")
            finding_ids.add(finding.id)
            for j, report_id in enumerate(finding.affected):
                if report_id not in requirements:
                    error(("set_findings", i, "affected", j), report_id, f"неизвестное требование {report_id}")

        for i, row in enumerate(self.compliance):
            for j, finding_id in enumerate(row.findings):
                if finding_id not in finding_ids:
                    error(("compliance", i, "findings", j), finding_id, f"неизвестное замечание {finding_id}")

        if missing := requirements - checked:
            ids = ", ".join(sorted(missing))
            error(("checks",), ids, f"отсутствуют проверки для {ids}")

        if errors:
            raise ValidationError.from_exception_data(type(self).__name__, errors)
        return self
