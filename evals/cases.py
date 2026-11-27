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
    individual_review_passage: str | None = None


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

OFFICIAL_REGRESSION_CASES = (
    EvalCase(
        id="official_second_notification",
        description="D-20: the monthly cap and the threshold-change rule disagree after a first notification.",
        passages=(
            Passage("monthly-cap", "На карту приходится максимум одно уведомление о достижении порога за календарный месяц.", "Расходы"),
            Passage("threshold-change", "При изменении порога накопленная сумма сохраняется. Если она ниже нового порога, следующее достижение вызывает уведомление независимо от истории предыдущей настройки.", "Настройки"),
        ),
        expected=(ExpectedIssue(QualityCriterion.CONSISTENCY, (frozenset({"monthly-cap", "threshold-change"}),)),),
    ),
    EvalCase(
        id="official_purchase_month",
        description="D-33: a purchase and its posting straddle the month boundary.",
        passages=(
            Passage("purchase-date-rule", "Месяц расходов определяется по дате совершения покупки по времени Алматы. В расходы входят проведённые покупки.", "Расчёт"),
            Passage("month-end-acceptance", """Все даты и время в примере указаны по Алматы.

| Исходные условия | Действие | Ожидаемый результат |
| --- | --- | --- |
| Покупка 31 января в 23:58 на 4 000 ₸ | Провести 1 февраля в 00:03 | Февральский счётчик вырос на 4 000 ₸ |""", "Приёмка"),
        ),
        expected=(ExpectedIssue(QualityCriterion.CONSISTENCY, (frozenset({"purchase-date-rule", "month-end-acceptance"}),)),),
    ),
    EvalCase(
        id="official_settings_read",
        description="D-29: the screen needs stored settings and monthly spend, but the API describes saving only.",
        passages=(
            Passage("screen-values", "При открытии экрана отображаются сохранённый порог, состояние переключателя и сумма расходов за текущий месяц.", "Интерфейс"),
            Passage("integration", "Приложение читает и сохраняет настройки карты через дочернюю спецификацию REST API.", "Интеграция"),
            Passage("save-contract", "POST /alerts/limit: заменить настройку карты и вернуть результат сохранения.", "REST API"),
        ),
        expected=(ExpectedIssue(
            QualityCriterion.COMPLETENESS,
            (frozenset({"integration", "save-contract"}), frozenset({"screen-values", "save-contract"})),
            manual_review=True,
        ),),
    ),
    EvalCase(
        id="official_purchase_lifecycle",
        description="D-15: the trigger for spending and the notification deadline is unclear across transaction stages.",
        passages=(
            Passage("notification-goal", "Клиент узнаёт о достижении личного бюджета в момент покупки.", "Цель"),
            Passage("transaction-stages", "Процессинг авторизует покупку и позднее выполняет клиринг.", "Процессинг"),
            Passage("purchase-trigger", "В расходы входят проведённые покупки. Процессинг передаёт сервису расходов событие «проведена покупка».", "Учёт расходов"),
            Passage("dispatch-window", "Сервис передаёт уведомление на отправку в течение минуты после получения события.", "Уведомления"),
        ),
        expected=(ExpectedIssue(
            QualityCriterion.AMBIGUITY,
            (frozenset({"transaction-stages", "purchase-trigger", "notification-goal"}),
             frozenset({"transaction-stages", "purchase-trigger", "dispatch-window"})),
        ),),
    ),
    EvalCase(
        id="official_refund_attribution",
        description="D-26: the refund flow lacks a defined way to identify the original purchase month.",
        passages=(
            Passage("refund-rule", "Возврат уменьшает расходы месяца исходной покупки независимо от даты возврата.", "Расчёт"),
            Passage("refund-event", "Для возврата процессинг передаёт идентификатор карты, идентификатор операции возврата, дату и сумму возврата.", "Событие возврата"),
            Passage("recorded-purchases", "Для проведённой покупки сервис хранит идентификатор карты, идентификатор операции, месяц покупки и сумму.", "Данные"),
        ),
        expected=(ExpectedIssue(
            QualityCriterion.COMPLETENESS,
            (frozenset({"refund-rule", "refund-event"}),),
            manual_review=True,
        ),),
    ),
    EvalCase(
        id="official_amount_response_type",
        description="The request and response independently specify different types for amount.",
        passages=(
            Passage("request-field", "Запрос POST /alerts/limit принимает поле amount типа integer. Значение задаёт порог в целых тенге.", "Запрос"),
            Passage("response-field", "При успехе HTTP 200 возвращает сохранённый порог в поле amount типа string.", "Ответ"),
            Passage("response-example", 'Пример успешного ответа: {"amount":"50000"}.', "Пример ответа"),
        ),
        expected=(),
        individual_review_passage="response-field",
    ),
    EvalCase(
        id="official_resolved_purchase_trigger",
        description="An explicit event emission rule fixes the trigger and the start of the dispatch deadline.",
        passages=(
            Passage("transaction-stages", "Процессинг авторизует покупку и позднее выполняет клиринг.", "Процессинг"),
            Passage("purchase-trigger", "Событие «проведена покупка» публикуется только после клиринга. При получении события сервис включает покупку в расходы.", "Учёт расходов"),
            Passage("dispatch-window", "Если расходы достигли порога, сервис передаёт уведомление на отправку в течение минуты после получения события «проведена покупка».", "Уведомления"),
        ),
        expected=(),
    ),
)

RESPONSE_SCHEMA_CONFLICT = EvalCase(
    id="response_type_conflicts_with_example",
    description="The successful response example contradicts the type declared for that response.",
    passages=(
        Passage("response-field", "В успешном ответе поле edition передаётся целым числом.", "Ответ"),
        Passage("response-example", 'Пример успешного ответа: {"edition":"7"}.', "Пример ответа"),
    ),
    expected=(ExpectedIssue(QualityCriterion.CONSISTENCY, (frozenset({"response-field", "response-example"}),)),),
    individual_review_passage="response-field",
)

ALL_CASES = CASES + OFFICIAL_REGRESSION_CASES + (RESPONSE_SCHEMA_CONFLICT,)

