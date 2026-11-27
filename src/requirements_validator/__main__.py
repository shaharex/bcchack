import argparse
import logging
import os
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import ExitStack, closing
from importlib.metadata import version
from pathlib import Path

from dotenv import dotenv_values
from pydantic import TypeAdapter, ValidationError
from report_template.render import EXPORT_FORMATS, prepare_format

from .checkpoint import CheckpointedLLM
from .clarifications import StakeholderClarificationGenerator
from .gemini import GeminiClient, GeminiSettings, LLMError
from .models import QualityCriterion
from .parsers import DocumentParseError, FileDocumentParser
from .pipeline import AnalysisPipeline
from .ports import StructuredResponseError
from .report import MarkdownReportGenerator
from .report_export import TemplateReportGenerator
from .severity import DEFAULT_RULES, Level, RubricSeverityClassifier, SeverityRule
from .source import EvidenceError

LOG = logging.getLogger(__name__)
GEMINI_ENV = {
    "api_key": "GEMINI_API_KEY", "model": "GEMINI_MODEL",
    "timeout_ms": "GEMINI_TIMEOUT_MS", "max_attempts": "GEMINI_MAX_ATTEMPTS",
    "max_retry_wait_seconds": "GEMINI_MAX_RETRY_WAIT_SECONDS",
}


def main(argv: Sequence[str] | None = None) -> int:
    cli = argparse.ArgumentParser(prog="python -m requirements_validator",
                                  description="Validate requirements and write a Russian report.")
    cli.add_argument("--input", type=Path, required=True, help="UTF-8 Markdown or text-based PDF")
    cli.add_argument("-o", "--output", type=Path, help="Destination report (default: report.<format>)")
    formats = ("md", *EXPORT_FORMATS)
    cli.add_argument("-f", "--format", choices=formats,
                     help="Report format (default: md, or the format from --output)")
    cli.add_argument("--no-resume", action="store_true", help="Run without reading or writing local checkpoints")
    args = cli.parse_args(argv)
    extension = args.output.suffix.lower().removeprefix(".") if args.output else ""
    if args.format and extension in formats and extension != args.format:
        cli.error(f"--format {args.format} does not match the .{extension} output extension.")
    report_format = args.format or (extension if extension in formats else "md")
    output = args.output or Path(f"report.{report_format}")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    checkpoint = None
    try:
        if not args.input.is_file():
            raise DocumentParseError(f"Input file does not exist or is not a file: {args.input}")
        if output.exists() and (output.is_dir() or args.input.samefile(output)):
            raise DocumentParseError("Output must be a file distinct from the input.")
        if not output.parent.is_dir():
            raise DocumentParseError(f"Output directory does not exist: {output.parent}")
        if report_format == "md":
            writer = MarkdownReportGenerator()
        else:
            prepare_format(report_format)
            writer = TemplateReportGenerator(args.input, report_format)
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
        document = FileDocumentParser().parse(args.input)
        checkpoint_path = output.with_name(f".{output.name}.gemini-checkpoint.sqlite3")
        LOG.info("Analyzing %s with %s", args.input.name, settings.model)
        with ExitStack() as resources:
            live = resources.enter_context(GeminiClient(settings))
            llm = live
            if not args.no_resume:
                if checkpoint_path.exists() and args.input.samefile(checkpoint_path):
                    raise DocumentParseError("Checkpoint path must be distinct from the input.")
                database = resources.enter_context(closing(sqlite3.connect(checkpoint_path)))
                checkpoint = CheckpointedLLM(live, database, document_id=document.id, agent_version=agent_version)
                llm = checkpoint
            pipeline = AnalysisPipeline(
                llm, agent_version=agent_version, severity=rubric, clarification=StakeholderClarificationGenerator(llm),
            )
            try:
                result = pipeline.analyze(document)
            except (StructuredResponseError, EvidenceError):
                # A terminal validation error would repeat on every resume if the
                # same problematic answers stayed in the checkpoint.
                if checkpoint is not None:
                    checkpoint.finish()
                    checkpoint = None
                raise
            writer.write(result, output)
            if checkpoint is not None:
                if checkpoint.reused:
                    LOG.info("Resumed using %d saved Gemini replies.", checkpoint.reused)
                checkpoint.finish()
        LOG.info("Wrote %s (%d requirements, %d findings)", output,
                 len(result.requirements), len(result.review.findings))
    except ValidationError as exc:
        fields = ", ".join(GEMINI_ENV.get(str(item["loc"][0]), "analysis data") if item["loc"] else "analysis data"
                           for item in exc.errors(include_input=False))
        print(f"Error: Invalid configuration or analysis data ({fields}).", file=sys.stderr)
        return 1
    except LLMError as exc:
        print(f"Error: Gemini analysis failed. {exc}", file=sys.stderr)
        if checkpoint is not None and (checkpoint.reused or checkpoint.saved):
            print("Progress saved. Re-run the same command after Gemini access is restored.", file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
