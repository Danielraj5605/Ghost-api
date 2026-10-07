"""Friendly error messages for common failures."""

import socket
import sqlite3
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError

from ghost_api.errors import friendly_error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            PlaywrightError("BrowserType.launch: Executable doesn't exist at C:/x/chrome.exe"),
            "playwright install chromium",
        ),
        (
            PlaywrightError("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://nope.invalid/"),
            "address could not be found",
        ),
        (
            PlaywrightError("Page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:1/"),
            "refused the connection",
        ),
        (PlaywrightError("Page.goto: Timeout 30000ms exceeded."), "took too long"),
        (PlaywrightError("Page.goto: net::ERR_UNSAFE_PORT at http://127.0.0.1:9/"), "port"),
        (sqlite3.OperationalError("database is locked"), "another ghost-api command"),
    ],
)
def test_known_errors_are_explained(error: Exception, expected: str) -> None:
    assert expected in friendly_error(error)


def test_unknown_errors_keep_their_first_line() -> None:
    assert friendly_error(ValueError("first line\nsecond line")) == "first line"
    assert friendly_error(ValueError("")) == "ValueError"


def test_unreachable_site_gives_a_clear_message(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ghost_api.cli import app

    with socket.socket() as sock:  # a port that was free a moment ago: nothing listens on it
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = CliRunner().invoke(
        app,
        [
            "run",
            f"http://127.0.0.1:{port}/",
            "--goal",
            "x",
            "--no-agent",
            "--ignore-robots",
            "--db",
            str(tmp_path / "t.db"),
            "--out",
            str(tmp_path / "o"),
        ],
    )
    assert "refused the connection" in result.output
    assert "Traceback" not in result.output


def test_output_that_cannot_show_symbols_does_not_crash() -> None:
    """Found while recording the README: `ghost-api ui > log.txt` crashed on '◆'."""
    import io

    from ghost_api.cli import safe_output

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    safe_output(stream)
    stream.write("◆ Ghost API dashboard")
    stream.flush()
    assert raw.getvalue() == b"? Ghost API dashboard"
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    safe_output(utf8)
    assert utf8.errors == "strict"  # UTF-8 output is left alone
