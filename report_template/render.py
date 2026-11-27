"""Подготовка шаблона и запись отчёта в выбранный формат."""

from base64 import b64encode
from collections import Counter
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from os import replace
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import ValidationError

from .schema import Report, Severity

ExportFormat = Literal["pdf", "docx", "html"]
EXPORT_FORMATS = ("pdf", "docx", "html")
ROOT = Path(__file__).resolve().parent
SEVERITIES: tuple[Severity, ...] = ("Критично", "Существенно", "Незначительно")
CLEAR = "Замечаний нет"
BADGE_CLASSES = dict(zip((*SEVERITIES, CLEAR), ("critical", "major", "minor", "clear"), strict=True))
JSON_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.IGNORECASE | re.DOTALL)


class RenderError(ValueError):
    """Ошибка настройки экспорта, которую можно показать в CLI."""


@dataclass(frozen=True)
class Statistics:
    requirements: int
    findings: int
    by_severity: dict[Severity, int]
    critical: list[tuple[str, str]]
    clean_requirements: int


def statistics(report: Report) -> Statistics:
    findings = [finding for check in report.checks for finding in check.findings]
    all_findings = [*findings, *report.set_findings]
    counts = Counter(finding.severity for finding in all_findings)
    return Statistics(
        requirements=len(report.requirements),
        findings=len(all_findings),
        by_severity={severity: counts[severity] for severity in SEVERITIES},
        critical=[(finding.id, " ".join(finding.explanation.split()))
                  for finding in all_findings if finding.severity == "Критично"],
        clean_requirements=sum(not check.findings for check in report.checks),
    )


def load_report(path: Path) -> Report:
    raw = path.read_text(encoding="utf-8-sig").strip()
    match = JSON_FENCE.fullmatch(raw)
    return Report.model_validate_json(match[1] if match else raw)


def validation_message(exc: ValidationError) -> str:
    messages = {
        "missing": "обязательное поле",
        "extra_forbidden": "лишнее поле",
        "string_type": "ожидается строка",
        "list_type": "ожидается массив",
        "bool_type": "ожидается true или false",
        "model_type": "ожидается объект",
        "literal_error": "допустимые значения: " + ", ".join(SEVERITIES),
        "string_pattern_mismatch": "недопустимый формат ID",
        "json_invalid": "некорректный JSON",
    }
    lines = ["Ошибки во входном JSON:"]
    for error in exc.errors(include_url=False, include_input=False):
        path = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"])
        message = messages.get(error["type"], error["msg"].removeprefix("Value error, "))
        if error["type"] == "json_invalid":
            message += f" ({error['ctx']['error']})"
        lines.append(f"- {path.lstrip('.') or 'JSON'}: {message}")
    return "\n".join(lines)


@cache
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(ROOT), autoescape=select_autoescape(("j2",)),
        undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True,
    )


@cache
def _stylesheet() -> str:
    css = (ROOT / "styles.css").read_text(encoding="utf-8")
    for filename in ("PTSans-Regular.ttf", "PTSans-Bold.ttf"):
        path = f"fonts/{filename}"
        encoded = b64encode((ROOT / path).read_bytes()).decode("ascii")
        css = css.replace(path, f"data:font/ttf;base64,{encoded}")
    return css


def render_html(report: Report, *, styled: bool = True) -> str:
    stats = statistics(report)
    by_id = {check.requirement_id: check.findings for check in report.checks}
    rank = {severity: index for index, severity in enumerate(SEVERITIES)}
    checks = []
    for requirement in report.requirements:
        findings = by_id[requirement.report_id]
        severity = min((finding.severity for finding in findings), key=rank.__getitem__, default=CLEAR)
        checks.append((requirement, findings, severity))
    return _environment().get_template("template.html.j2").render(
        report=report, stats=stats, critical=stats.critical, checks=checks, badge_classes=BADGE_CLASSES,
        metrics=[("Требований", stats.requirements), ("Нарушений", stats.findings), *stats.by_severity.items()],
        stylesheet=_stylesheet() if styled else "",
    )


@cache
def prepare_format(format: ExportFormat) -> None:
    if format == "pdf":
        try:
            import_module("weasyprint")
        except (ImportError, OSError) as exc:
            raise RenderError(
                "Для PDF установите WeasyPrint и Pango по инструкции report_template/README.md."
            ) from exc
    elif format == "docx":
        try:
            import_module(".render_docx", __package__)
        except ImportError as exc:
            raise RenderError("Для DOCX установите python-docx по инструкции report_template/README.md.") from exc
    elif format != "html":
        raise RenderError(f"Неизвестный формат: {format}")


def write_report(report: Report, destination: Path, format: ExportFormat) -> None:
    prepare_format(format)
    rendered = render_html(report, styled=format != "docx")
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=destination.parent, prefix=".requirements-report-", delete=False) as handle:
            temporary = Path(handle.name)
        if format == "pdf":
            from weasyprint import HTML
            from weasyprint.text.fonts import FontConfiguration
            from weasyprint.urls import URLFetcher

            HTML(string=rendered, url_fetcher=URLFetcher(allowed_protocols=("data",))).write_pdf(
                temporary, font_config=FontConfiguration(),
            )
        elif format == "docx":
            from .render_docx import write_docx

            write_docx(rendered, temporary)
        else:
            temporary.write_text(rendered, encoding="utf-8")
        replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
