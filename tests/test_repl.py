"""The interactive session (`ghost-api` with no command): stays open until /exit or
Ctrl+C twice, runs commands in-process, and links results."""

import io
from pathlib import Path

import pytest
from rich.console import Console

from ghost_api.ui.repl import Repl, _find_url, run_session
from ghost_api.ui.terminal import file_link, file_url


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> None:
        self.calls.append(args)


def make(
    tmp_path: Path, answers: list[str] | None = None
) -> tuple[Repl, Recorder, list[str], io.StringIO]:
    out = io.StringIO()
    console = Console(file=out, width=400, color_system=None)
    recorder = Recorder()
    opened: list[str] = []
    pending = list(answers or [])
    repl = Repl(console, recorder, lambda q: pending.pop(0) if pending else "",
                out=tmp_path / "output", open_url=opened.append)  # fmt: skip
    return repl, recorder, opened, out


@pytest.mark.parametrize(
    ("line", "url"),
    [
        ("quotes.toscrape.com/scroll list quotes with authors", "quotes.toscrape.com/scroll"),
        ("https://www.google.com/maps find places", "https://www.google.com/maps"),
        ("get venues from turftown.in/chennai please", "turftown.in/chennai"),
        ("http://127.0.0.1:8000/shop list products", "http://127.0.0.1:8000/shop"),
        ("open report.html and models.py", None),
        ("list quotes", None),
        ("mail me at a@b.com", None),
    ],
)
def test_find_url(line: str, url: str | None) -> None:
    assert _find_url(line) == url


def test_url_and_goal_start_a_run_and_the_session_continues(tmp_path: Path) -> None:
    repl, calls, _, out = make(tmp_path)
    assert repl.handle("quotes.toscrape.com/scroll list quotes with their authors") is True
    assert calls.calls == [["run", "https://quotes.toscrape.com/scroll", "--goal",
                            "list quotes with their authors", "--lang", "python"]]  # fmt: skip
    assert repl.site == "quotes.toscrape.com/scroll"
    assert "/fetch" in out.getvalue() and "/exit" in out.getvalue()


def test_missing_goal_is_asked_and_more_words_reuse_the_site(tmp_path: Path) -> None:
    repl, calls, _, _ = make(tmp_path, answers=["list quotes"])
    repl.handle("https://quotes.toscrape.com/scroll")
    assert calls.calls[-1][3] == "list quotes"
    repl.handle("now the authors' birthplaces")
    assert calls.calls[-1][:4] == ["run", "https://quotes.toscrape.com/scroll", "--goal",
                                   "now the authors' birthplaces"]  # fmt: skip


def test_words_without_a_site_get_a_hint(tmp_path: Path) -> None:
    repl, calls, _, out = make(tmp_path)
    repl.handle("list quotes")
    assert calls.calls == [] and "Paste a website" in out.getvalue()


def test_slash_commands(tmp_path: Path) -> None:
    repl, calls, opened, out = make(tmp_path)
    repl.handle("/fetch")
    assert calls.calls == [] and "Which site?" in out.getvalue()
    repl.handle("turftown.in/chennai find venues")
    repl.handle("/fetch get_nearby_venues area=velachery --format xlsx")
    assert calls.calls[-1] == ["fetch", "turftown.in/chennai", "get_nearby_venues",
                               "area=velachery", "--format", "xlsx"]  # fmt: skip
    repl.handle("/fetch quotes.toscrape.com/scroll")
    assert calls.calls[-1] == ["fetch", "quotes.toscrape.com/scroll"]
    repl.handle("/watch")
    assert calls.calls[-1] == ["watch", "list"]
    repl.handle('/watch add turftown.in/chennai get_nearby_venues "latLong=[12.9,80.2]"')
    assert calls.calls[-1][-1] == "latLong=[12.9,80.2]"
    repl.handle("/lang typescript")
    repl.handle("/agent off")
    repl.handle("quotes.toscrape.com/scroll list quotes")
    assert calls.calls[-1][-3:] == ["--lang", "typescript", "--no-agent"]
    repl.handle("/lang cobol")
    assert "Use /lang python" in out.getvalue()
    repl.handle("/fethc")
    assert "Did you mean /fetch" in out.getvalue()
    repl.handle("/help")
    assert "Ctrl+C twice" in out.getvalue()
    assert repl.handle("/exit") is False and repl.handle("/quit") is False


def test_open_links_the_report(tmp_path: Path) -> None:
    repl, _, opened, out = make(tmp_path)
    repl.handle("/open")
    assert "No report yet" in out.getvalue()
    report = tmp_path / "output" / "quotes.toscrape.com" / "scroll" / "report.html"
    report.parent.mkdir(parents=True)
    report.write_text("<html></html>", encoding="utf-8")
    repl.handle("/open quotes.toscrape.com/scroll")
    assert opened == [report.resolve().as_uri()]
    assert report.resolve().as_uri() in out.getvalue()


def test_file_links() -> None:
    url = file_url("output/x/report.html")
    assert url.startswith("file:///") and url.endswith("/output/x/report.html")
    text = file_link("output/x/report.html")
    assert text.plain == url and f"link {url}" in str(text.style)


def test_the_input_loop_needs_ctrl_c_twice(tmp_path: Path) -> None:
    """Typed lines go to the session; one Ctrl+C doesn't end it, two in a row do."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    repl, calls, _, out = make(tmp_path)
    with create_pipe_input() as pipe:
        pipe.send_text("quotes.toscrape.com/scroll list quotes\r")
        pipe.send_text("\x03")  # one Ctrl+C: still here
        pipe.send_text("/watch\r")
        pipe.send_text("\x03\x03")  # twice: goodbye
        run_session(repl.console, repl, has_llm=True, prompt_input=pipe,
                    prompt_output=DummyOutput())  # fmt: skip
    assert [c[0] for c in calls.calls] == ["run", "watch"]
    text = out.getvalue()
    assert "Welcome to Ghost API" in text and "Goodbye" in text


def test_the_input_loop_ends_on_exit(tmp_path: Path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    repl, calls, _, out = make(tmp_path)
    with create_pipe_input() as pipe:
        pipe.send_text("/exit\r")
        pipe.send_text("quotes.toscrape.com/scroll never runs\r")
        run_session(repl.console, repl, has_llm=False, prompt_input=pipe,
                    prompt_output=DummyOutput())  # fmt: skip
    assert calls.calls == [] and "no LLM key" in out.getvalue()


def test_commands_run_in_process_without_ending_the_session(tmp_path: Path) -> None:
    from ghost_api.cli import _invoke

    _invoke(["watch", "list", "--db", str(tmp_path / "missing.db")])  # prints, doesn't exit
    _invoke(["fetch"])  # missing argument: an error message, not SystemExit
    _invoke(["no-such-command"])


def test_a_real_run_inside_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo shop, typed into the session (agent off: no LLM), then more commands."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from evals.demo_shop import serve as serve_shop
    from ghost_api import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GHOST_API_DB", str(tmp_path / "ghost_api.db"))
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400, color_system=None))
    shop = serve_shop(0)
    url = f"http://127.0.0.1:{shop.server_address[1]}/"
    try:
        repl = Repl(cli.console, cli._invoke, lambda q: "", out=Path("output"))
        with create_pipe_input() as pipe:
            pipe.send_text("/agent off\r")
            pipe.send_text(f"{url} list products\r")
            pipe.send_text("/sites\r")
            pipe.send_text("/exit\r")
            run_session(cli.console, repl, has_llm=True, prompt_input=pipe,
                        prompt_output=DummyOutput())  # fmt: skip
    finally:
        shop.shutdown()
    text = buffer.getvalue()
    report = (tmp_path / "output" / "127.0.0.1" / "report.html").resolve()
    assert report.exists()
    assert report.as_uri() in text  # the clickable link, not just a path
    assert "127.0.0.1" in text.split("Report:")[-1] and "Goodbye" in text
