"""Преобразует проверенный результат агента во входные данные шаблона."""

from collections import defaultdict
from pathlib import Path

from report_template.render import SEVERITIES, ExportFormat, write_report
from report_template.schema import Check, Conclusion, Finding, Passport, Report, Requirement, SetFinding, Severity

from .models import AnalysisRunResult
from .report import CRITERIA, report_readiness
from .severity import RubricSeverityClassifier
from .source import SourceIndex

SEVERITY_LABELS: dict[str, Severity] = {
    "critical": "Критично", "high": "Критично", "medium": "Существенно", "low": "Незначительно",
}


def to_report(result: AnalysisRunResult, source: Path) -> Report:
    index = SourceIndex(result.document)
    report_ids = {item.id: f"R-{position:03}" for position, item in enumerate(result.requirements, 1)}
    requirements = [
        Requirement(
            report_id=report_ids[item.id],
            doc_id=item.original_id.quote if item.original_id is not None else None,
            section=" → ".join(item.section) or result.document.title,
            summary=item.statement,
            quote=item.source.quote,
        )
        for item in result.requirements
    ]

    decisions = result.severities
    if decisions is None:
        classifier = RubricSeverityClassifier()
        decisions = tuple(classifier.classify(finding) for finding in result.review.findings)
    severities = {decision.finding_id: SEVERITY_LABELS[decision.level] for decision in decisions}
    individual: dict[str, list[Finding]] = defaultdict(list)
    whole: list[SetFinding] = []
    priorities: list[tuple[int, str]] = []
    for finding in result.review.findings:
        severity = severities[finding.id]
        criterion = CRITERIA[finding.criterion]
        if "specification" in finding.scopes or len(finding.requirement_ids) != 1:
            report_id = f"ДН-{len(whole) + 1:02}"
            whole.append(SetFinding(
                id=report_id, severity=severity, criterion=criterion,
                affected=[report_ids[owner] for owner in finding.requirement_ids],
                explanation=finding.explanation, fix=finding.recommendation,
            ))
        else:
            owner = finding.requirement_ids[0]
            requirement_id = report_ids[owner]
            report_id = f"{requirement_id}.{len(individual[requirement_id]) + 1}"
            evidence = next(ref for ref in finding.evidence if ref.requirement_id == owner)
            span = evidence.location
            block = index.blocks[span.block_id]
            section = " → ".join(block.section) or result.document.title
            page = f"стр. {block.page}, " if block.page is not None else ""
            location = f"{section} · {page}{block.id}, символы [{span.start}, {span.end})"
            individual[requirement_id].append(Finding(
                id=report_id, severity=severity, criterion=criterion, location=location, quote=evidence.quote,
                explanation=finding.explanation, fix=finding.recommendation,
            ))
        priorities.append((SEVERITIES.index(severity), f"{report_id} · {finding.recommendation}"))
    priorities.sort(key=lambda entry: entry[0])
    can_start, reason = report_readiness(result, has_blocking_findings="Критично" in severities.values())
    return Report(
        passport=Passport(
            document_title=result.document.title, source=str(source), run_date=result.started_at.isoformat(),
            model=result.model, agent_version=result.agent_version,
        ),
        requirements=requirements,
        checks=[Check(requirement_id=item.report_id, findings=individual[item.report_id]) for item in requirements],
        set_findings=whole,
        conclusion=Conclusion(can_start=can_start, reason=reason, priorities=[text for _, text in priorities]),
    )


class TemplateReportGenerator:
    def __init__(self, source: Path, format: ExportFormat) -> None:
        self.source = source
        self.format = format

    def write(self, result: AnalysisRunResult, destination: Path) -> None:
        write_report(to_report(result, self.source), destination, self.format)
