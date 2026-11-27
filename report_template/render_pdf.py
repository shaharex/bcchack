"""CLI для экспорта готового JSON без запуска проверки требований."""

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from pydantic import ValidationError

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report_template.render import EXPORT_FORMATS, load_report, validation_message, write_report


def main(argv: Sequence[str] | None = None, *, pdf_only: bool = True) -> int:
    cli = argparse.ArgumentParser(
        prog=None if pdf_only else "python -m report_template",
        description="Создать отчёт о проверке требований из готового JSON.",
    )
    cli.add_argument("input", type=Path, metavar="report.json", help="JSON с данными отчёта")
    cli.add_argument("-o", "--output", type=Path, help="Путь к файлу отчёта")
    if not pdf_only:
        cli.add_argument("-f", "--format", choices=EXPORT_FORMATS, help="Формат отчёта (по умолчанию PDF)")
    args = cli.parse_args(argv)
    suffix = args.output.suffix.lower().removeprefix(".") if args.output else ""
    format = "pdf" if pdf_only else args.format or (suffix if suffix in EXPORT_FORMATS else "pdf")
    if suffix in EXPORT_FORMATS and suffix != format:
        cli.error(f"Формат {format} не соответствует расширению .{suffix}.")
    destination = args.output or args.input.with_suffix(f".{format}")
    try:
        if destination.exists() and destination.samefile(args.input):
            raise ValueError("Путь отчёта должен отличаться от входного JSON.")
        write_report(load_report(args.input), destination, format)
    except ValidationError as exc:
        print(validation_message(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


def run_cli(*, pdf_only: bool = True) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    return main(pdf_only=pdf_only)


if __name__ == "__main__":
    raise SystemExit(run_cli())
