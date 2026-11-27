"""Создаёт воспроизводимый пример с 40 требованиями и 60 замечаниями."""

import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    example = json.loads((root / "sample_report.json").read_text(encoding="utf-8"))
    requirements = []
    checks = []
    for number in range(1, 41):
        report_id = f"R-{number:02}"
        section = f"3.{number} Переговорная {number:02}"
        quote = f"Сотрудник может забронировать переговорную {number:02} на доступный интервал."
        requirements.append({
            "report_id": report_id, "doc_id": None if number % 3 == 0 else f"БР-{number:02}",
            "section": section, "summary": quote, "quote": quote,
        })
        findings = []
        if number <= 24:
            for position, finding in enumerate(example["checks"][0]["findings"], 1):
                findings.append({
                    **finding, "id": f"{report_id}.{position}", "location": f"{section}, абзац 1",
                    "quote": quote,
                })
        checks.append({"requirement_id": report_id, "findings": findings})

    set_findings = []
    for number in range(1, 13):
        finding = example["set_findings"][(number - 1) % len(example["set_findings"])]
        set_findings.append({
            **finding, "id": f"ДН-{number:02}", "affected": [f"R-{number:02}", f"R-{number + 24:02}"],
        })

    example["passport"]["document_title"] = "Бронирование переговорных учебного центра"
    example["passport"]["source"] = "training_center_booking.md"
    example.update(requirements=requirements, checks=checks, set_findings=set_findings)
    example["conclusion"]["priorities"] = [
        "R-01.1 · Определить порядок обработки одновременных запросов.",
        "ДН-01 · Уточнить правила освобождения интервала после отмены.",
    ]
    (root / "sample_report_large.json").write_text(
        json.dumps(example, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n",
    )


if __name__ == "__main__":
    main()
