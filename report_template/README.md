# Шаблон отчёта о проверке требований

Модуль преобразует готовый JSON в PDF, редактируемый DOCX или HTML. Он работает локально и не требует доступа к Gemini. PDF собирается через Jinja2 и WeasyPrint с кириллическим шрифтом PT Sans. Файлы шрифта и лицензия SIL OFL входят в комплект. [Источник шрифта](https://github.com/google/fonts/tree/main/ofl/ptsans)

## Установка

Нужен Python 3.11 или новее. Установите зависимости из корня репозитория.

```sh
python -m pip install -r report_template/requirements.txt
```

Для PDF нужны системные библиотеки Pango и HarfBuzz. Команда установки для Debian и Ubuntu приведена ниже.

```sh
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0
```

На Windows x64 установите MSYS2 и обновите пакеты по инструкции установщика. Команду ниже выполните в терминале **MSYS2 UCRT64**. [MSYS2](https://www.msys2.org/)

```sh
pacman -S --needed mingw-w64-ucrt-x86_64-pango
```

Затем в PowerShell с активированным окружением Python укажите каталог DLL. Команда `weasyprint --info` покажет версию Pango.

```powershell
$env:WEASYPRINT_DLL_DIRECTORIES = 'C:\msys64\ucrt64\bin'
python -m weasyprint --info
```

При другом расположении MSYS2 замените путь. Переменная PowerShell действует в текущем сеансе. [Установка WeasyPrint на Windows](https://doc.courtbouillon.org/weasyprint/v70.0/first_steps.html#windows)

## Запуск на готовых данных

```sh
python report_template/render_pdf.py report_template/sample_report.json -o sample.pdf
python -m report_template report_template/sample_report.json --format docx -o sample.docx
python -m report_template report_template/sample_report.json --format html -o sample.html
```

Из каталога `report_template/` можно запустить `python render_pdf.py report.json -o report.pdf`. CLI принимает UTF-8 JSON, в том числе с BOM и внутри блока кода Markdown с языком `json`. Ошибки валидации выводятся с путями полей, например `checks[0].findings[0].severity`.

## Формат JSON

| Поле | Содержимое |
| --- | --- |
| `passport` | Строки `document_title`, `source`, `run_date`, `model`, `agent_version`. |
| `requirements[]` | `report_id` в формате `R-01`, `doc_id` как строка или `null`, `section`, `summary` и дословная `quote`, обязательная при `doc_id: null`. |
| `checks[]` | По одной записи на требование. Каждая запись содержит `requirement_id` и массив `findings`. Поля замечания: `id` в формате `R-01.1`, `severity`, `criterion`, `location`, `quote`, `explanation`, `fix`. |
| `set_findings[]` | `id` в формате `ДН-01`, `severity`, `criterion`, массив `affected` с ID затронутых требований, `explanation` и `fix`. |
| `conclusion` | Булево `can_start`, строка `reason` и массив строк `priorities`. |

Поле `severity` принимает значения `Критично`, `Существенно` и `Незначительно`. ID требований и замечаний уникальны. Для требования без индивидуальных замечаний передайте `findings: []`. Этот массив определяет счётчик «Требований без замечаний». Замечания к набору входят в общие счётчики нарушений и серьёзности. Сводка вычисляется из входных данных.

## Подключение к агенту

Основной CLI передаёт готовый `AnalysisRunResult` в адаптер `requirements_validator.report_export.to_report(result, source_path)`. Адаптер нумерует замечания и переводит уровни агента `critical/high`, `medium` и `low` в три уровня шаблона. Замечание к спецификации попадает в таблицу набора один раз, даже если анализ также привязал его к отдельному требованию.

Чтобы использовать ту же схему в структурированном ответе Gemini, передайте класс `report_template.schema.Report` как `response_schema` и примените `Report.model_validate_json` к готовому JSON. Pydantic дополнительно проверяет наличие цитат и связи между ID. [Поддержка Pydantic в SDK Gemini](https://googleapis.github.io/python-genai/#pydantic-model-schema-support)

Для вызова из Python без CLI используйте `report_template.render.write_report(report, destination, "pdf")`. Параметр `destination` принимает `pathlib.Path`.

## Примеры и тесты

В комплект входят исходный JSON, собранный PDF и большой пример с 40 требованиями и 60 замечаниями. [Исходный JSON](sample_report.json) · [PDF](sample.pdf) · [Большой пример](sample_report_large.json)

Команды ниже пересобирают большой пример и запускают тесты из корня репозитория.

```sh
python report_template/generate_stress_sample.py
python report_template/render_pdf.py report_template/sample_report_large.json -o sample_large.pdf
python -m pip install -e ".[test,export]"
python -m pytest tests/test_report_export.py -q
```
