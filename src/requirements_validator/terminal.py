import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from colorama import Fore, Style, just_fix_windows_console

LEVEL_COLORS = {
    logging.INFO: Fore.GREEN,
    logging.WARNING: Fore.YELLOW,
    logging.ERROR: Fore.RED,
    logging.CRITICAL: Fore.RED,
}


class TerminalFormatter(logging.Formatter):
    def __init__(self, *, color: bool) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stage = getattr(record, "stage", None)
        label = f"Part {stage}" if stage is not None else record.levelname
        prefix = f"{label:<8}  "
        message = super().format(record).replace("\n", "\n" + " " * len(prefix))
        color = Fore.LIGHTBLUE_EX if stage is not None else LEVEL_COLORS.get(record.levelno)
        if not self.color or not color:
            return prefix + message
        if stage is not None or record.levelno >= logging.ERROR:
            return f"{color}{prefix}{message}{Style.RESET_ALL}"
        return f"{color}{prefix}{Style.RESET_ALL}{message}"


@contextmanager
def terminal_logging() -> Iterator[None]:
    color = sys.stderr.isatty() and not os.getenv("NO_COLOR") and os.getenv("TERM") != "dumb"
    if color:
        just_fix_windows_console()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(TerminalFormatter(color=color))
    logger = logging.getLogger("requirements_validator")
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
