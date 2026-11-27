import logging
import sys

import pytest
from colorama import Fore

from requirements_validator import __main__ as cli
from requirements_validator import terminal


@pytest.mark.parametrize(("tty", "no_color"), [(True, False), (True, True), (False, False)])
def test_terminal_color_policy(capsys, monkeypatch, tty, no_color):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: tty)
    monkeypatch.setattr(terminal, "just_fix_windows_console", lambda: None)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")

    logger = logging.getLogger("requirements_validator.test")
    with terminal.terminal_logging():
        logger.info("Reading input")
        logger.info("Dividing file", extra={"stage": 1})
        logger.warning("Retrying")
        logger.error("Invalid file")

    lines = capsys.readouterr().err.splitlines()
    for line, label, color in zip(lines, ["INFO", "Part 1", "WARNING", "ERROR"],
                                  [Fore.GREEN, Fore.LIGHTBLUE_EX, Fore.YELLOW, Fore.RED], strict=True):
        assert line.startswith(color + label if tty and not no_color else label)
        if not tty or no_color:
            assert "\x1b" not in line


def test_invalid_arguments_keep_usage_and_exit_code(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])

    assert exc.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("usage:")
    assert "ERROR     one of the arguments" in output.err
