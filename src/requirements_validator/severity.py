from dataclasses import dataclass
from typing import Literal

from .models import Finding, QualityCriterion as Criterion, SeverityDecision

Level = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class SeverityRule:
    level: Level
    rationale: str

    def __post_init__(self) -> None:
        if self.level not in ("high", "medium", "low"):
            raise ValueError("severity rule must use high, medium or low")
        if not self.rationale.strip():
            raise ValueError("severity rule requires a rationale")


DEFAULT_RULES = {
    Criterion.AMBIGUITY: SeverityRule("medium", "Разные трактовки влияют на ожидаемое поведение."),
    Criterion.VERIFIABILITY: SeverityRule("medium", "Для проверки нужно определить наблюдаемый результат."),
    Criterion.COMPLETENESS: SeverityRule("medium", "Для реализации или тестирования нужно уточнить сценарий."),
    Criterion.CONSISTENCY: SeverityRule("high", "Несогласованные правила мешают выбрать единое поведение и ожидаемый результат теста."),
    Criterion.FEASIBILITY: SeverityRule("medium", "Нужно уточнить применимые ограничения и допустимое поведение."),
    Criterion.ATOMICITY: SeverityRule("low", "Разделение условий упростит их независимую проверку."),
    Criterion.IMPLEMENTATION_CONSTRAINT: SeverityRule("low", "Ограничение затрагивает выбор решения для указанного требования."),
    Criterion.NECESSITY: SeverityRule("low", "Нужно подтвердить, какой цели или потребности служит требование."),
    Criterion.CORRECTNESS: SeverityRule("medium", "Неверный факт в требовании приведёт к неверной реализации или тесту."),
    Criterion.CONFORMANCE: SeverityRule("low", "Документ отклоняется от собственного шаблона или соглашений."),
    Criterion.COMPREHENSIBILITY: SeverityRule("low", "Читатель может понять требования по-разному без определений и структуры."),
    Criterion.VALIDATABILITY: SeverityRule("medium", "Нельзя показать, что набор требований достигает заявленных целей."),
    Criterion.RANKING: SeverityRule("low", "Без приоритета и стабильности нельзя планировать объём и порядок работ."),
    Criterion.MODIFIABILITY: SeverityRule("low", "Изменение одного требования придётся вносить в нескольких местах."),
    Criterion.TRACEABILITY: SeverityRule("low", "Без идентификаторов и ссылок нельзя проследить покрытие требований."),
}

LOCAL_CONSISTENCY = SeverityRule("medium", "Нужно уточнить согласованность формулировки и ожидаемое поведение.")


class RubricSeverityClassifier:
    def __init__(self, overrides: dict[Criterion, SeverityRule] | None = None) -> None:
        self._overrides = dict(overrides or {})

    def classify(self, finding: Finding) -> SeverityDecision:
        rule = self._overrides.get(finding.criterion)
        if rule is None and finding.impact_class == "uncertain":
            return SeverityDecision(finding_id=finding.id, level="medium",
                                    rationale="Предварительная оценка. Масштаб подтверждённого последствия требует уточнения.")
        if rule is None and finding.impact_class is not None:
            level = {"blocks_behavior": "high", "needs_decision": "medium", "localized": "low"}[finding.impact_class]
            return SeverityDecision(finding_id=finding.id, level=level, rationale=finding.impact or finding.explanation)
        if rule is None:
            rule = DEFAULT_RULES[finding.criterion]
            if finding.criterion == Criterion.CONSISTENCY and len(finding.requirement_ids) < 2:
                # Same-block citations can still establish two competing rules.
                spans = [ref.location for ref in finding.evidence]
                distinct = any(
                    left.block_id != right.block_id or left.end <= right.start or right.end <= left.start
                    for i, left in enumerate(spans) for right in spans[i + 1:]
                )
                if not distinct:
                    rule = LOCAL_CONSISTENCY
        return SeverityDecision(finding_id=finding.id, level=rule.level, rationale=rule.rationale)
