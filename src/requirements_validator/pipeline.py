import logging
from datetime import UTC, datetime
from pathlib import Path

from .aggregation import FindingsAggregator
from .compliance import DocumentComplianceReviewer, document_evaluations
from .document_facts import measure
from .executable_review import ExecutableReviewer, ExecutionReview
from .language_lint import lint
from .extractor import RequirementsExtractor
from .models import AnalysisRunResult, ParsedDocument
from .ports import ClarificationGenerator, DocumentParser, ReportGenerator, SeverityClassifier, StructuredLLM
from .rule_probes import RuleProbes
from .reviewers import INDIVIDUAL_BATCH_SIZE, CrossRequirementReviewer, IndividualReviewer, SpecificationReview

LOG = logging.getLogger(__name__)

class AnalysisPipeline:
    def __init__(self, llm: StructuredLLM, *, agent_version: str,
                 parser: DocumentParser | None = None, severity: SeverityClassifier | None = None,
                 clarification: ClarificationGenerator | None = None, report: ReportGenerator | None = None,
                 executable_llm: StructuredLLM | None = None, executable: bool = True) -> None:
        self.extractor = RequirementsExtractor(llm)
        self.individual = IndividualReviewer(llm)
        self.cross = CrossRequirementReviewer(llm)
        self.aggregator = FindingsAggregator(llm)
        self.compliance = DocumentComplianceReviewer(llm)
        self.probes = RuleProbes(llm)
        self.executable_llm = executable_llm
        self.executable = ExecutableReviewer(executable_llm or llm) if executable else None
        self.llm = llm
        self.agent_version = agent_version
        self.parser = parser
        self.severity = severity
        self.clarification = clarification
        self.report = report

    def analyze(self, document: ParsedDocument) -> AnalysisRunResult:
        started_at = datetime.now(UTC)
        LOG.info("Extracting requirements", extra={"stage": 2})
        requirements, coverage = self.extractor.extract_with_coverage(document)
        LOG.info("Reviewing %d extracted requirements", len(requirements), extra={"stage": 3})
        individual = {}
        for start in range(0, len(requirements), INDIVIDUAL_BATCH_SIZE):
            targets = requirements[start:start + INDIVIDUAL_BATCH_SIZE]
            individual.update(self.individual.review_batch(document, targets, requirements))
            LOG.info("Reviewed %d of %d requirements", len(individual), len(requirements))
        LOG.info("Reviewing the full specification")
        cross = self.cross.review(document, requirements)
        LOG.info("Tracing required data and operations")
        dependencies = self.cross.review_dependencies(document, requirements)
        LOG.info("Reviewing focused cross-requirement scenarios")
        focused = self.cross.review_focused(document, requirements)
        LOG.info("Tracing relationships between source passages")
        relations = self.cross.review_relations(document, requirements)
        LOG.info("Probing output limits against overriding rules and reads against the documented operations")
        probes = self.probes.review(document, requirements)
        metrics = measure(document, requirements)
        LOG.info("Assessing the document against ISO/IEC/IEEE 29148 and IEEE 830")
        compliance = self.compliance.review(document, requirements, metrics)
        if self.executable is not None:
            LOG.info("Running scenarios against an executable model of the rules")
        execution = (self.executable.review(document, requirements) if self.executable is not None
                     else ExecutionReview(SpecificationReview(findings=()), None))
        proposals = (cross.findings + dependencies.findings + focused.findings + relations.findings
                     + probes + compliance.proposals().findings + lint(document, requirements)
                     + execution.findings.findings)
        findings = self.cross.repair_quotes(document, requirements, proposals)
        cross = SpecificationReview(findings=findings)
        LOG.info("Verifying finding evidence", extra={"stage": 4})
        review = self.aggregator.aggregate(document, requirements, individual, cross)
        review = review.model_copy(update={"document_evaluations": document_evaluations(compliance, review)})
        severities = None
        if self.severity is not None:
            severities = tuple(self.severity.classify(f) for f in review.findings)
            if any(d.finding_id != f.id for d, f in zip(severities, review.findings, strict=True)):
                raise ValueError("severity classifier returned a decision for another finding")
        result = AnalysisRunResult(
            document=document, started_at=started_at, model=self._model_label(), agent_version=self.agent_version,
            requirements=requirements, coverage=coverage, metrics=metrics, execution=execution.rows,
            review=review, severities=severities,
            questions=None,
        )
        if self.clarification is not None:
            LOG.info("Generating clarification questions for %d findings", len(review.findings))
            questions = self.clarification.generate(result)
            known = {f.id for f in review.findings}
            if any(not known.issuperset(q.finding_ids) for q in questions):
                raise ValueError("clarification references an unknown finding")
            result = result.model_copy(update={"questions": questions})
        return result

    def _model_label(self) -> str:
        if self.executable_llm is None or self.executable_llm.model == self.llm.model:
            return self.llm.model
        return f"{self.llm.model}; исполняемая проверка — {self.executable_llm.model}"

    def run(self, source: Path, destination: Path | None = None) -> AnalysisRunResult:
        if self.parser is None:
            raise ValueError("run() requires a DocumentParser")
        if destination is not None and self.report is None:
            raise ValueError("a report destination requires a ReportGenerator")
        result = self.analyze(self.parser.parse(source))
        if destination is not None and self.report is not None:
            self.report.write(result, destination)
        return result
