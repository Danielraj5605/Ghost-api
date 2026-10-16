"""Shared parts of the command line: the app, global options and helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from rich.console import Console

from ghost_api import __version__, exitcodes, workflows
from ghost_api.config import (
    check_language,
    default_db_path,
    load_env,
    state_dir,
)
from ghost_api.llm.providers import default_providers
from ghost_api.memory.store import Store, StoreError
from ghost_api.project import ProjectError, default_map, find_project
from ghost_api.site import SiteAmbiguous, resolve_site
from ghost_api.ui import keys
from ghost_api.ui.terminal import (
    TerminalRenderer,
)

app = typer.Typer(
    help="Ghost API: turn any website into a typed, tested API client.",
    add_completion=False,
)
console = Console()
_options = {"plain": False, "json": False}


def _version(value: bool) -> None:
    if value:
        console.print(f"ghost-api {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version", callback=_version, is_eager=True, expose_value=False, help="Show version."
        ),
    ] = None,
    plain: Annotated[
        bool, typer.Option("--plain", help="Simple line-by-line output (for logs and CI).")
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print the result as JSON on stdout (progress goes to stderr). "
            "Works anywhere on the command line.",
        ),
    ] = False,
) -> None:
    load_env()
    for stream in (sys.stdout, sys.stderr):
        safe_output(stream)
    _options["plain"] = plain or json_output
    _options["json"] = json_output
    if json_output:  # stdout is only for the JSON result; everything else goes to stderr
        saved = console._file  # usually None: Rich then follows sys.stdout as it changes
        console.file = sys.stderr
        ctx.call_on_close(lambda: setattr(console, "_file", saved))
    try:
        project = find_project()
    except ProjectError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=exitcodes.USAGE) from exc
    if project is not None:
        ctx.default_map = default_map(project, ctx.command)
    if ctx.invoked_subcommand is None:
        if not sys.stdin.isatty():
            console.print(ctx.get_help())
            raise typer.Exit()
        _interactive()


GLOBAL_FLAGS = ("--json", "--plain")


def _json_first(args: list[str]) -> list[str]:
    """`--json` and `--plain` may be typed anywhere (`ghost-api inspect --json`); they are
    global options, so they are moved in front of the command."""
    flags = [f for f in GLOBAL_FLAGS if f in args]
    return [*flags, *(a for a in args if a not in GLOBAL_FLAGS)] if flags else args


def json_out(data: object) -> None:
    """The command's result, as JSON on stdout (progress and messages are on stderr)."""
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def entry() -> None:
    """The `ghost-api` command: the app, with a plain message for a damaged database."""
    try:
        app(args=_json_first(sys.argv[1:]), prog_name="ghost-api")
    except StoreError as exc:
        console.print(f"[red]{exc}[/]")
        raise SystemExit(1) from None


def safe_output(stream: object) -> None:
    """Never crash on a character the output can't show (e.g. '◆' when output is
    redirected to a file on Windows, which uses cp1252): print a '?' instead."""
    if stream is None:  # pythonw / no console: nothing to protect
        return
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    reconfigure = getattr(stream, "reconfigure", None)
    if encoding != "utf8" and reconfigure is not None:
        reconfigure(errors="replace")


def _invoke(args: list[str]) -> None:
    """Run `ghost-api ARGS...` inside this process (the interactive session's commands)."""
    command = typer.main.get_command(app)
    try:
        command.main(args=_json_first(args), prog_name="ghost-api", standalone_mode=False)
    except typer.Abort:
        console.print("[yellow]Stopped.[/]")
    except typer.Exit:
        pass
    except StoreError as exc:
        console.print(f"[red]{exc}[/]")
    except typer.TyperException as exc:  # usage errors: missing argument, unknown option...
        format_message = getattr(exc, "format_message", None)
        message = format_message() if callable(format_message) else str(exc)
        console.print(f"[red]{message}[/]  [dim](see /help)[/]")


def _interactive() -> None:
    """`ghost-api` on its own: a session that stays open until /exit or Ctrl+C twice."""
    from ghost_api.ui.repl import Repl, run_session

    def ask(question: str) -> str:
        with keys.input_paused():
            return console.input(f"[bold]{question}[/]")

    def has_llm() -> bool:
        load_env()  # a key added to .env while the session is open counts at once
        return any(p.api_key for p in default_providers())

    repl = Repl(console, _invoke, ask)
    run_session(console, repl, has_llm, history_file=state_dir() / "history.txt")


class _RunResult(workflows.RunCollector):
    """A run's outcome for `--json`, and its exit code."""

    def finish(self, db: Path) -> None:
        """Print the JSON (with --json) and exit with the code for the run's status."""
        status = str(self.data["status"])
        if _options["json"]:
            json_out(self.data | {"database": str(db), "exit_code": _run_exit(status)})
        if _run_exit(status):
            raise typer.Exit(code=_run_exit(status))


def _run_exit(status: str) -> int:
    return exitcodes.for_run_status(status)


def _renderer() -> TerminalRenderer:
    return TerminalRenderer(console, plain=True if _options["plain"] else None)


def _normalize_url(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise typer.BadParameter(f"Not a valid http(s) URL: {url}")
    return url


def _check_lang(lang: str) -> str:
    try:
        return check_language(lang)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _not_allowed(url: str) -> None:
    if _options["json"]:
        json_out({"status": "not-allowed", "url": url, "reason": "disallowed by robots.txt",
                  "exit_code": exitcodes.NOT_ALLOWED})  # fmt: skip
    raise typer.Exit(code=exitcodes.NOT_ALLOWED)


def _need_db(db: Path | None) -> Path:
    path = db or default_db_path()
    if not path.exists():
        console.print(f"No database at {path}. Run `ghost-api run` first.")
        raise typer.Exit(code=1)
    return path


SITE_HELP = "Site explored before, e.g. quotes.toscrape.com or www.google.com/maps"


def _site(store: Store, name: str) -> str:
    """The stored site for what was typed: a site, a URL, or just the domain."""
    try:
        return resolve_site(name, {r.domain for r in store.list_runs()})
    except SiteAmbiguous as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
