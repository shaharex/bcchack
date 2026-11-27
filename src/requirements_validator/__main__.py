import argparse
import logging
import os
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import NoReturn

from dotenv import dotenv_values
from pydantic import TypeAdapter, ValidationError
from report_template.render import EXPORT_FORMATS, prepare_format

from .checkpoint import CheckpointedLLM
from .clarifications import StakeholderClarificationGenerator
from .gemini import GeminiClient, GeminiSettings
from .groq import GROQ_ENV, GroqClient, GroqSettings
from .mistral import MISTRAL_ENV, MistralClient, MistralSettings
from .models import AnalysisRunResult, QualityCriterion
from .parsers import DocumentParseError, FileDocumentParser
from .pipeline import AnalysisPipeline
from .ports import LLMError, StructuredResponseError
from .report import MarkdownReportGenerator, write_text_atomic
from .report_export import TemplateReportGenerator
from .severity import DEFAULT_RULES, Level, RubricSeverityClassifier, SeverityRule
from .source import EvidenceError
from .terminal import terminal_logging

LOG = logging.getLogger("requirements_validator.cli")
GEMINI_ENV = {
    "api_key": "GEMINI_API_KEY", "model": "GEMINI_MODEL",
    "timeout_ms": "GEMINI_TIMEOUT_MS", "max_attempts": "GEMINI_MAX_ATTEMPTS",
    "max_retry_wait_seconds": "GEMINI_MAX_RETRY_WAIT_SECONDS",
}


def source_version() -> str:
    """Return the agent's git tag or commit, marked dirty for uncommitted changes."""
    package = Path(__file__).resolve().parent
    try:
        described = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"], cwd=package,
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        described = ""
    return described or f"v{version('requirements-validator')}"


class TerminalArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        LOG.error("%s", message)
        self.exit(2)


def main(argv: Sequence[str] | None = None) -> int:
    with terminal_logging():
        return _run(argv)


def _run(argv: Sequence[str] | None) -> int:
    cli = TerminalArgumentParser(prog="python -m requirements_validator",
                                 description="Validate requirements and write a Russian report.")
    source = cli.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="UTF-8 Markdown or text-based PDF")
    source.add_argument("--analysis-input", type=Path, help="Render Markdown from saved analysis JSON without an API call")
    cli.add_argument("--analysis-output", type=Path, help="Save the validated analysis to a JSON file")
    cli.add_argument("-o", "--output", type=Path, help="Destination report (default: report.<format>)")
    formats = ("md", *EXPORT_FORMATS)
    cli.add_argument("-f", "--format", choices=formats,
                     help="Report format (default: md, or the format from --output)")
    providers = {
        "gemini": ("Gemini", GEMINI_ENV, GeminiSettings, GeminiClient),
        "mistral": ("Mistral", MISTRAL_ENV, MistralSettings, MistralClient),
        "groq": ("Groq", GROQ_ENV, GroqSettings, GroqClient),
    }
    cli.add_argument("--provider", choices=tuple(providers), default="gemini",
                     help="Model provider (default: gemini)")
    cli.add_argument("--no-resume", action="store_true", help="Run without reading or writing local checkpoints")
    cli.add_argument("--diagnostics", action="store_true", help="Append coverage and citation coordinates to Markdown")
    args = cli.parse_args(argv)
    extension = args.output.suffix.lower().removeprefix(".") if args.output else ""
    if args.format and extension in formats and extension != args.format:
        cli.error(f"--format {args.format} does not match the .{extension} output extension.")
    report_format = args.format or (extension if extension in formats else "md")
    if args.analysis_input and args.analysis_output:
        cli.error("--analysis-output requires --input.")
    if (args.analysis_input or args.diagnostics) and report_format != "md":
        cli.error("--analysis-input and --diagnostics are available for Markdown reports.")
    output = args.output or Path(f"report.{report_format}")
    checkpoint = None
    provider_name, provider_env, settings_type, client_type = providers[args.provider]
    try:
        input_path = args.input or args.analysis_input
        if not input_path.is_file():
            raise DocumentParseError(f"Input file does not exist or is not a file: {input_path}")
        if output.exists() and (output.is_dir() or input_path.samefile(output)):
            raise DocumentParseError("Output must be a file distinct from the input.")
        if not output.parent.is_dir():
            raise DocumentParseError(f"Output directory does not exist: {output.parent}")
        checkpoint_path = output.with_name(f".{output.name}.{args.provider}-checkpoint.sqlite3")
        if args.analysis_output:
            paths = (input_path, output, checkpoint_path)
            if any(args.analysis_output.resolve() == path.resolve() or
                   (args.analysis_output.exists() and path.exists() and args.analysis_output.samefile(path))
                   for path in paths):
                raise DocumentParseError("Analysis output must be distinct from the input, report and checkpoint.")
            if args.analysis_output.is_dir() or not args.analysis_output.parent.is_dir():
                raise DocumentParseError("Analysis output must be a file in an existing directory.")
        if args.analysis_input:
            LOG.info("Loading saved analysis from %s", input_path, extra={"stage": 1})
            result = AnalysisRunResult.model_validate_json(input_path.read_text(encoding="utf-8-sig"))
            LOG.info("Writing Markdown report", extra={"stage": 2})
            MarkdownReportGenerator(diagnostics=args.diagnostics).write(result, output, rendered_at=datetime.now(UTC))
            LOG.info("Rendered %s from saved analysis", output)
            return 0
        if report_format == "md":
            writer = MarkdownReportGenerator(diagnostics=args.diagnostics)
        else:
            prepare_format(report_format)
            writer = TemplateReportGenerator(args.input, report_format)
        file_env = dotenv_values(Path.cwd() / ".env", encoding="utf-8-sig", interpolate=False)
        env = {key: value for key, value in file_env.items() if value is not None}
        env.update(os.environ)
        try:
            settings = settings_type.from_env(env)
        except ValidationError as exc:
            for issue in exc.errors(include_input=False):
                field = ".".join(map(str, issue["loc"]))
                LOG.error("Invalid %s configuration (%s). %s",
                          provider_name, provider_env.get(field, field or ".env"), issue["msg"])
            return 1
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
        agent_version = env.get("VALIDATOR_AGENT_VERSION", "").strip() or source_version()
        LOG.info("Analyzing %s with %s (%s)", input_path.name, provider_name, settings.model)
        LOG.info("Dividing file into source blocks", extra={"stage": 1})
        document = FileDocumentParser().parse(input_path)
        LOG.info("Read %d source blocks", len(document.blocks))
        with ExitStack() as resources:
            live = resources.enter_context(client_type(settings))
            llm = live
            executable_llm = None
            executable_model = env.get("EXECUTABLE_GEMINI_MODEL", "").strip()
            if executable_model and args.provider == "gemini":
                # The hardest translation step may use a stronger model; the main chain is its fallback.
                executable_settings = settings.model_copy(update={
                    "model": executable_model,
                    "fallback_models": tuple(m for m in (settings.model, *settings.fallback_models) if m != executable_model),
                })
                executable_llm = resources.enter_context(GeminiClient(executable_settings))
            if not args.no_resume:
                if checkpoint_path.exists() and args.input.samefile(checkpoint_path):
                    raise DocumentParseError("Checkpoint path must be distinct from the input.")
                database = resources.enter_context(closing(sqlite3.connect(checkpoint_path)))
                checkpoint = CheckpointedLLM(live, database, document_id=document.id, agent_version=agent_version)
                llm = checkpoint
                if executable_llm is not None:
                    executable_llm = CheckpointedLLM(executable_llm, database, document_id=document.id,
                                                     agent_version=agent_version)
            pipeline = AnalysisPipeline(
                llm, agent_version=agent_version, severity=rubric, clarification=StakeholderClarificationGenerator(llm),
                executable_llm=executable_llm,
                executable=env.get("VALIDATOR_EXECUTABLE", "1").strip().casefold() not in {"0", "false", "no", "off"},
            )
            try:
                result = pipeline.analyze(document)
            except (StructuredResponseError, EvidenceError):
                # A terminal validation error would repeat on every resume if the
                # same problematic answers stayed in the checkpoint.
                for saved in (checkpoint, executable_llm):
                    if isinstance(saved, CheckpointedLLM):
                        saved.finish()
                checkpoint = None
                raise
            LOG.info("Writing %s report", report_format.upper(), extra={"stage": 5})
            writer.write(result, output)
            if args.analysis_output:
                result.check_references()
                write_text_atomic(args.analysis_output, result.model_dump_json(indent=2) + "\n")
                LOG.info("Saved analysis to %s", args.analysis_output)
            if checkpoint is not None:
                if checkpoint.reused:
                    LOG.info("Resumed using %d saved %s replies.", checkpoint.reused, provider_name)
                checkpoint.finish()
                if isinstance(executable_llm, CheckpointedLLM):
                    executable_llm.finish()
                checkpoint = None
        LOG.info("Wrote %s (%d requirements, %d findings)", output,
                 len(result.requirements), len(result.review.findings))
    except ValidationError as exc:
        issue = exc.errors(include_input=False)[0]
        field = ".".join(map(str, issue["loc"])) or "document"
        LOG.error("Invalid analysis data at %s. %s", field, issue["msg"])
        return 1
    except LLMError as exc:
        LOG.error("%s analysis failed. %s", provider_name, exc)
        if checkpoint is not None and (checkpoint.reused or checkpoint.saved):
            LOG.warning("Progress saved. Re-run the same command after %s access is restored.", provider_name)
        return 1
    except KeyboardInterrupt:
        LOG.error("Run interrupted.")
        if checkpoint is not None and (checkpoint.reused or checkpoint.saved):
            LOG.warning("Progress saved. Re-run the same command to resume.")
        return 130
    except sqlite3.Error as exc:
        LOG.error("Could not use the local checkpoint. %s", exc)
        return 1
    except EvidenceError as exc:
        LOG.error("Evidence validation failed. %s", exc)
        return 1
    except (OSError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
