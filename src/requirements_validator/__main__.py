import argparse
import logging
import os
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

from dotenv import dotenv_values
from pydantic import TypeAdapter, ValidationError

from .clarifications import StakeholderClarificationGenerator
from .gemini import GeminiClient, GeminiSettings, LLMError
from .models import QualityCriterion
from .parsers import DocumentParseError, FileDocumentParser
from .pipeline import AnalysisPipeline
from .report import MarkdownReportGenerator
from .severity import DEFAULT_RULES, Level, RubricSeverityClassifier, SeverityRule

LOG = logging.getLogger(__name__)
GEMINI_ENV = {
    "api_key": "GEMINI_API_KEY", "model": "GEMINI_MODEL",
    "timeout_ms": "GEMINI_TIMEOUT_MS", "max_attempts": "GEMINI_MAX_ATTEMPTS",
}


def main(argv: Sequence[str] | None = None) -> int:
    cli = argparse.ArgumentParser(prog="python -m requirements_validator",
                                  description="Validate requirements and write a Russian Markdown report.")
    cli.add_argument("--input", type=Path, required=True, help="UTF-8 Markdown or text-based PDF")
    cli.add_argument("--output", type=Path, required=True, help="Destination Markdown report")
    args = cli.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        if not args.input.is_file():
            raise DocumentParseError(f"Input file does not exist or is not a file: {args.input}")
        if args.output.exists() and (args.output.is_dir() or args.input.samefile(args.output)):
            raise DocumentParseError("Output must be a file distinct from the input.")
        if not args.output.parent.is_dir():
            raise DocumentParseError(f"Output directory does not exist: {args.output.parent}")
        file_env = dotenv_values(Path.cwd() / ".env", encoding="utf-8-sig", interpolate=False)
        env = {key: value for key, value in file_env.items() if value is not None}
        env.update(os.environ)
        settings = GeminiSettings.from_env(env)
        try:
            overrides = TypeAdapter(dict[QualityCriterion, Level]).validate_json(
                env.get("VALIDATOR_SEVERITY_OVERRIDES", "{}")
            )
        except ValidationError:
            raise DocumentParseError(
                "Invalid VALIDATOR_SEVERITY_OVERRIDES. Use a JSON object mapping criteria to high, medium or low."
            ) from None
        rubric = RubricSeverityClassifier({
            criterion: SeverityRule(level, DEFAULT_RULES[criterion].rationale)
            for criterion, level in overrides.items()
        })
        agent_version = env.get("VALIDATOR_AGENT_VERSION", "").strip() or f"v{version('requirements-validator')}"
        LOG.info("Analyzing %s with %s", args.input.name, settings.model)
        with GeminiClient(settings) as llm:
            pipeline = AnalysisPipeline(
                llm, agent_version=agent_version, parser=FileDocumentParser(), severity=rubric,
                clarification=StakeholderClarificationGenerator(llm), report=MarkdownReportGenerator(),
            )
            result = pipeline.run(args.input, args.output)
        LOG.info("Wrote %s (%d requirements, %d findings)", args.output,
                 len(result.requirements), len(result.review.findings))
    except ValidationError as exc:
        fields = ", ".join(GEMINI_ENV.get(str(item["loc"][0]), "analysis data") if item["loc"] else "analysis data"
                           for item in exc.errors(include_input=False))
        print(f"Error: Invalid configuration or analysis data ({fields}).", file=sys.stderr)
        return 1
    except LLMError as exc:
        print(f"Error: Gemini analysis failed. {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
