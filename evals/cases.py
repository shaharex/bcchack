"""Small, synthetic specifications and their passage-level semantic oracles."""

from dataclasses import dataclass

from requirements_validator.models import QualityCriterion


@dataclass(frozen=True)
class Passage:
    id: str
    text: str
    section: str


@dataclass(frozen=True)
class ExpectedIssue:
    criterion: QualityCriterion
    # Each inner set is a complete acceptable evidence set.  Passage IDs are
    # deliberately used instead of expected wording for the finding itself.
    acceptable_passage_sets: tuple[frozenset[str], ...]
    manual_review: bool = False


@dataclass(frozen=True)
class EvalCase:
    id: str
    description: str
    passages: tuple[Passage, ...]
    expected: tuple[ExpectedIssue, ...]


CASES = (
    EvalCase(
        id="conflicting_limits",
        description="Competing notification caps apply to the same card and month.",
        passages=(
            Passage("monthly-cap", "На одну карту отправляется не более одного уведомления за календарный месяц.", "Лимиты"),
            Passage("changed-limit", "После изменения порога его следующее достижение всегда вызывает новое уведомление, независимо от ранее отправленных уведомлений.", "Настройки"),
        ),
        expected=(ExpectedIssue(QualityCriterion.CONSISTENCY, (frozenset({"monthly-cap", "changed-limit"}),)),),
    ),
    EvalCase(
        id="purchase_vs_processing_date",
        description="A boundary transaction is assigned to two different months.",
        passages=(
            Passage("purchase-rule", "Месяц расходов определяется по дате совершения покупки в часовом поясе Алматы.", "Расчёт"),
            Passage("processing-example", "Покупка совершена 31 января и обработана 1 февраля; сумма включается в февральский счётчик.", "Приёмка"),
        ),
        expected=(ExpectedIssue(QualityCriterion.CONSISTENCY, (frozenset({"purchase-rule", "processing-example"}),)),),
    ),
    EvalCase(
        id="http_200_error_vs_400",
        description="The same invalid request has incompatible HTTP statuses.",
        passages=(
            Passage("invalid-rule", "Если сумма порога некорректна, сервис возвращает HTTP 400.", "Валидация"),
            Passage("error-example", "Пример ответа для некорректной суммы: HTTP 200 {\"result\":\"ERROR\",\"message\":\"Проверьте сумму\"}.", "API"),
        ),
        expected=(ExpectedIssue(QualityCriterion.CONSISTENCY, (frozenset({"invalid-rule", "error-example"}),)),),
    ),
    EvalCase(
        id="missing_read_operation",
        description="The UI must show persisted state but the contract only exposes a write operation.",
        passages=(
            Passage("display-current", "При открытии экрана приложение должно показать ранее сохранённый порог пользователя.", "Интерфейс"),
            Passage("write-only", "Контракт сервиса содержит единственную операцию POST /limits для сохранения нового порога.", "API"),
        ),
        expected=(ExpectedIssue(QualityCriterion.COMPLETENESS, (frozenset({"display-current", "write-only"}),), manual_review=True),),
    ),
    EvalCase(
        id="missing_data_dependencies",
        description="A calculation is required without a stated source for its input events.",
        passages=(
            Passage("calculation", "Сервис должен вычислять сумму покупок по карте за текущий календарный месяц.", "Расчёт"),
            Passage("available-input", "На вход сервиса поступают только идентификатор карты и установленный пользователем порог.", "Интеграция"),
        ),
        expected=(ExpectedIssue(QualityCriterion.COMPLETENESS, (frozenset({"calculation", "available-input"}),), manual_review=True),),
    ),
    EvalCase(
        id="resolved_cross_context_boundary",
        description="Numeric endpoint inclusion is resolved in another section.",
        passages=(
            Passage("range-rule", "Сервис принимает сумму перевода в пределах диапазона, заданного тарифом карты.", "Переводы"),
            Passage("standard-bounds", "Для тарифа Standard нижняя граница суммы перевода равна 1000 тенге, верхняя — 500000 тенге.", "Тарифы"),
            Passage("inclusive-boundaries", "Для всех числовых диапазонов нижняя и верхняя границы включаются в допустимый диапазон.", "Общие правила валидации"),
        ),
        expected=(),
    ),
    EvalCase(
        id="lifecycle_ambiguity",
        description="An event stage is ambiguous and changes an observable SLA.",
        passages=(
            Passage("completion-sla", "Клиенту отправляется уведомление не позднее 60 секунд после завершения перевода.", "Уведомления"),
            Passage("transfer-stages", "Перевод сначала авторизуется банком, а затем окончательно рассчитывается после клиринга.", "Жизненный цикл перевода"),
            Passage("stage-delay", "Между авторизацией и окончательным расчётом может пройти до двух рабочих дней.", "Клиринг"),
        ),
        expected=(ExpectedIssue(
            QualityCriterion.AMBIGUITY,
            (frozenset({"completion-sla", "transfer-stages", "stage-delay"}),),
        ),),
    ),
    EvalCase(
        id="clean_valid_requirement",
        description="A complete, internally consistent requirement should remain clean.",
        passages=(
            Passage("valid-rule", "После успешного POST /limits сервис сохраняет целое значение от 1000 до 500000 тенге и возвращает HTTP 204 не позднее чем за 500 мс.", "API"),
            Passage("valid-errors", "Значение вне диапазона 1000–500000 тенге отклоняется с HTTP 400 и не сохраняется.", "API"),
        ),
        expected=(),
    ),
)

