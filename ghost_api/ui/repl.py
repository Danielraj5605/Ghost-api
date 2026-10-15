"""The interactive session: `ghost-api` with no command, like Claude Code or Gemini CLI.

Paste a website and what you want ("quotes.toscrape.com/scroll list quotes with authors"),
watch it work, then keep going: download the data, set up alerts, open the report, try
another site. The session ends only on /exit or Ctrl+C pressed twice; a single Ctrl+C
stops what is running and brings the prompt back.

`Repl.handle()` turns one line into actions (and is what the tests drive); `run_session()`
adds the input box: history, slash-command completion and the hints line.
"""

from __future__ import annotations

import os
import re
import shlex
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghost_api import __version__
from ghost_api.site import site_key
from ghost_api.ui.terminal import ACCENT, file_link, file_url

EXIT_WINDOW_S = 2.0  # Ctrl+C twice within this many seconds ends the session
URL_RE = re.compile(
    r"(?<![\w@])((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/[^\s]*)?)", re.I
)
FILE_SUFFIXES = (".py", ".md", ".json", ".csv", ".html", ".txt", ".xlsx")
LOCAL_URL_RE = re.compile(r"(?<![\w@])(https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/\S*)?)")


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    help: str
    cli: str | None = None  # the `ghost-api` command it runs, if any
    needs_site: bool = False  # uses the current site when none is given


COMMANDS = [
    Command("/help", "/help", "Show commands and tips"),
    Command("/sites", "/sites", "Sites you've explored", cli="inspect"),
    Command("/fetch", "/fetch [SITE] [OPERATION] [name=value ...] [--format xlsx]",
            "Download data as CSV, Excel or JSON (no code)", cli="fetch", needs_site=True),
    Command("/watch", "/watch add|list|run|changes|remove ...",
            "Alerts when items appear, change or disappear", cli="watch"),
    Command("/report", "/report [SITE]", "Write the site's report and link it",
            cli="report", needs_site=True),
    Command("/open", "/open [SITE]", "Open the site's report in your browser"),
    Command("/refresh", "/refresh [SITE]", "Re-check a known site from memory",
            cli="refresh", needs_site=True),
    Command("/generate", "/generate [SITE] [--lang typescript]", "Regenerate the client",
            cli="generate", needs_site=True),
    Command("/serve", "/serve [SITE]", "Serve sites as a local REST API (Ctrl+C stops it)",
            cli="serve"),
    Command("/mock", "/mock [SITE]", "Offline copy of the site's API (Ctrl+C stops it)",
            cli="mock", needs_site=True),
    Command("/ui", "/ui", "Open the web dashboard (Ctrl+C stops it)", cli="ui"),
    Command("/inspect", "/inspect [SITE] [--run N]", "What's stored about a site or a run",
            cli="inspect"),
    Command("/doctor", "/doctor", "Check your LLM keys", cli="doctor"),
    Command("/forget", "/forget SITE [--llm-cache]", "Delete everything stored about a site",
            cli="forget"),
    Command("/lang", "/lang python|typescript", "Client language for new runs"),
    Command("/agent", "/agent on|off", "Use the AI agent for new runs, or just load the page"),
    Command("/clear", "/clear", "Clear the screen"),
    Command("/exit", "/exit", "End the session (or press Ctrl+C twice)"),
]  # fmt: skip
BY_NAME = {c.name: c for c in COMMANDS} | {"/quit": COMMANDS[-1], "/q": COMMANDS[-1]}


class Repl:
    def __init__(
        self,
        console: Console,
        invoke: Callable[[list[str]], None],
        ask: Callable[[str], str],
        out: Path = Path("output"),
        open_url: Callable[[str], object] = webbrowser.open,
    ) -> None:
        self.console = console
        self.invoke = invoke  # runs `ghost-api ARGS...` in this process
        self.ask = ask
        self.out = out
        self.open_url = open_url
        self.lang = "python"
        self.agent = True
        self.site: str | None = None  # the current site
        self.url: str | None = None

    # --- one line ---

    def handle(self, line: str) -> bool:
        """Do what the line says. Returns False when the session should end."""
        line = line.strip()
        if not line:
            return True
        if line.startswith("/"):
            return self._command(line)
        url = _find_url(line)
        if url:
            goal = line.replace(url, " ").strip(" -:·,")
            return self._explore(url, goal)
        if self.url:  # more words about the current site: a new goal for it
            self.console.print(Text(f"New goal for {self.site}: {line}", style="dim"))
            return self._explore(self.url, line)
        self.console.print(
            "Paste a website and what you want from it, e.g. "
            "[bold]quotes.toscrape.com/scroll list quotes with their authors[/], "
            "or type [bold]/help[/]."
        )
        return True

    def _explore(self, url: str, goal: str) -> bool:
        if "://" not in url:
            url = "https://" + url
        if not goal:
            goal = self.ask("What do you want from this site? ").strip()
            if not goal:
                self.console.print("[dim]No goal given; nothing started.[/]")
                return True
        self.url, self.site = url, site_key(url)
        args = ["run", url, "--goal", goal, "--lang", self.lang]
        if not self.agent:
            args.append("--no-agent")
        self.invoke(args)
        self._after_run()
        return True

    def _after_run(self) -> None:
        report = self._report_path()
        if report is not None:
            self.console.print(
                Text("Report: ", style="bold") + file_link(report) + Text("  (/open)", style="dim")
            )
            self.console.print(
                Text(
                    f"Next: /fetch to download {self.site}'s data · /watch add for alerts · "
                    "another URL to explore · /exit to finish",
                    style="dim",
                )  # fmt: skip
            )
        else:
            # No report generated — likely no endpoints found
            self.console.print(
                Text(
                    "No endpoints found this time. Try a different goal, or paste a new URL. "
                    "Type /doctor to check your LLM keys.",
                    style="yellow",
                )
            )
            self.console.print(
                Text(
                    f"Next: /fetch {self.site} to see what's available · another URL to explore "
                    "· /exit to finish",
                    style="dim",
                )
            )

    def _command(self, line: str) -> bool:
        name, _, rest = line.partition(" ")
        command = BY_NAME.get(name.lower())
        if command is None:
            from difflib import get_close_matches

            close = get_close_matches(name.lower(), [c.name for c in COMMANDS], n=3, cutoff=0.5)
            hint = f" Did you mean {', '.join(close)}?" if close else ""
            self.console.print(f"Unknown command {name}.{hint} Type /help for the list.")
            return True
        if command.name == "/exit":
            return False
        if command.name == "/help":
            self.console.print(help_panel())
        elif command.name == "/clear":
            self.console.clear()
        elif command.name == "/lang":
            self._set_lang(rest.strip())
        elif command.name == "/agent":
            self._set_agent(rest.strip().lower())
        elif command.name == "/open":
            self._open(rest.strip())
        elif command.cli:
            self._run_cli(command, rest)
        return True

    def _run_cli(self, command: Command, rest: str) -> None:
        try:
            args = shlex.split(rest, posix=os.name != "nt")
        except ValueError as exc:
            self.console.print(f"Couldn't read that: {exc}")
            return
        args = [a.strip('"') if os.name == "nt" else a for a in args]
        if command.needs_site and not _starts_with_site(args):
            if self.site is None:
                self.console.print(f"Which site? Usage: {command.usage}")
                return
            args = [self.site, *args]
        if command.name == "/watch" and not args:
            args = ["list"]
        if command.needs_site and args and "://" in args[0]:
            site = site_key(args[0])
            if not (self.out / site).exists():
                self.console.print(
                    f"{site} hasn't been explored yet. To explore it, paste the URL with what "
                    f"you want (no /command), e.g. [bold]{args[0]} list the headlines[/]"
                )
                return
            args = [site, *args[1:]]
        if args and command.needs_site:
            self.site = args[0] if "/" in args[0] or "." in args[0] else self.site
        self.invoke([command.cli or "", *args])

    def _set_lang(self, lang: str) -> None:
        if lang not in ("python", "typescript"):
            self.console.print(
                f"Client language is {self.lang}. Use /lang python or /lang typescript."
            )
            return
        self.lang = lang
        self.console.print(f"New runs will generate a {lang} client.")

    def _set_agent(self, value: str) -> None:
        if value not in ("on", "off"):
            state = "on" if self.agent else "off"
            self.console.print(f"The AI agent is {state}. Use /agent on or /agent off.")
            return
        self.agent = value == "on"
        self.console.print(
            "New runs use the AI agent."
            if self.agent
            else "New runs only load and scroll the page (no LLM calls)."  # fmt: skip
        )

    def _open(self, site: str) -> None:
        if site:
            self.site = site
        report = self._report_path()
        if report is None:
            self.console.print("No report yet. Explore a site first (paste its URL).")
            return
        self.open_url(file_url(report))
        self.console.print(Text("Opened ", style="dim") + file_link(report))

    def _report_path(self) -> Path | None:
        if not self.site:
            return None
        for name in ("report.html", "report.md"):
            path = self.out / self.site / name
            if path.exists():
                return path
        return None


def _find_url(line: str) -> str | None:
    local = LOCAL_URL_RE.search(line)
    if local:
        return local.group(1)
    for match in URL_RE.finditer(line):
        if line[match.end() : match.end() + 1] == "@":
            continue  # the name part of an email address (john.doe@example.com)
        candidate = match.group(1).rstrip(".,;)")
        host = candidate.split("://")[-1].split("/")[0].split(":")[0]
        is_file = host.endswith(FILE_SUFFIXES)
        if "." in host and not host.replace(".", "").isdigit() and not is_file:
            return candidate
    return None


def _starts_with_site(args: list[str]) -> bool:
    return bool(args) and not args[0].startswith("-") and ("." in args[0] or "://" in args[0])


# --- the screen -----------------------------------------------------------------------------


def welcome_panel(cwd: Path, has_llm: bool) -> Panel:
    body = Group(
        Text.assemble(
            ("◆ ", ACCENT), ("Welcome to Ghost API", "bold"), (f"  v{__version__}", "dim")
        ),
        Text(""),
        Text("  Paste a website and say what data you want.", style="dim"),
        Text("  /help for commands · Ctrl+C twice to exit", style="dim"),
        Text(""),
        Text("  AI agent: ready", style="dim")
        if has_llm
        else Text("  AI agent: no LLM key in .env (pages are only loaded and scrolled)",
                  style="yellow"),  # fmt: skip
    )  # fmt: skip
    return Panel(body, border_style=ACCENT, padding=(0, 1), expand=False)


def tips() -> Text:
    return Text.assemble(
        ("Tips for getting started:\n", "bold"),
        ("1. ", "dim"), ("quotes.toscrape.com/scroll list quotes with their authors\n", ""),
        ("2. ", "dim"), ("/fetch", ACCENT), (" to download a site's data, ", ""),
        ("/watch add", ACCENT), (" for alerts\n", ""),
        ("3. ", "dim"), ("/ui", ACCENT), (" opens the dashboard, ", ""),
        ("/sites", ACCENT), (" lists what you've explored", ""),
    )  # fmt: skip


def help_panel() -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=f"bold {ACCENT}", no_wrap=True)
    table.add_column()
    for c in COMMANDS:
        table.add_row(c.name, Text.assemble((c.help, ""), (f"\n{c.usage}", "dim")))
    body = Group(
        Text(
            "Paste a URL and a goal to explore a site. More words after a run give it a new "
            "goal on the same site.\n",
            style="dim",
        ),  # fmt: skip
        table,
        Text(
            "\nCtrl+C stops what's running. Ctrl+C twice (or /exit) ends the session. "
            "Up/down arrows: history. Tab: complete a command.",
            style="dim",
        ),  # fmt: skip
    )
    return Panel(body, title="Commands", title_align="left", border_style=ACCENT, padding=(1, 2))


# --- the input loop -------------------------------------------------------------------------


def run_session(
    console: Console,
    repl: Repl,
    has_llm: bool | Callable[[], bool],
    history_file: Path | None = None,
    prompt_input: object | None = None,
    prompt_output: object | None = None,
) -> None:
    """Read lines until /exit or Ctrl+C twice. `prompt_input`/`prompt_output` are for tests."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.styles import Style

    from ghost_api.ui import keys

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
            word = document.text_before_cursor
            if not word.startswith("/") or " " in word:
                return
            for c in COMMANDS:
                if c.name.startswith(word):
                    yield Completion(c.name, start_position=-len(word), display_meta=c.help)

    if history_file is not None:
        history_file.parent.mkdir(parents=True, exist_ok=True)
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_file)) if history_file else InMemoryHistory(),
        completer=SlashCompleter(),
        complete_while_typing=True,
        style=Style.from_dict({"rule": "#5a5a70", "prompt": f"{ACCENT} bold", "hint": "#8a8aa0"}),
        input=prompt_input,  # type: ignore[arg-type]
        output=prompt_output,  # type: ignore[arg-type]
    )
    state = {"armed": 0.0}

    def toolbar() -> HTML:
        width = max(20, console.width)
        if time.monotonic() - state["armed"] < EXIT_WINDOW_S:
            hint = "Press Ctrl+C again to exit"
        else:
            site = f" · site: {repl.site}" if repl.site else ""
            agent = "" if repl.agent else " · agent off"
            hint = f"/help for commands · {repl.lang}{agent}{site} · Ctrl+C twice to exit"
        return HTML(f"<rule>{'─' * width}</rule>\n<hint>  {_escape(hint)}</hint>")

    from ghost_api.ui import terminal

    def ask(question: str) -> str:
        """A follow-up question, in the same input box (Backspace, arrows... all work).
        Ctrl+C or an empty answer cancels."""
        try:
            return session.prompt(
                HTML(f"<prompt>{_escape(question)}</prompt>"),
                bottom_toolbar=HTML("<hint>  Ctrl+C to cancel</hint>"),
            )
        except (KeyboardInterrupt, EOFError):
            return ""

    plain_invoke = repl.invoke

    def invoke(args: list[str]) -> None:
        # Only while something runs: Ctrl+C becomes a key (see ui/keys.py).
        terminal.PROGRESS.update(site="", step=0, endpoints=0)
        with keys.ctrl_c_as_key():
            plain_invoke(args)

    plain_ask = repl.ask
    repl.ask, repl.invoke = ask, invoke
    keys.on_stop_requested = lambda message: console.print(f"[yellow]{message}[/]")
    terminal.SESSION["active"] = True
    try:
        console.print(welcome_panel(Path.cwd(), has_llm() if callable(has_llm) else has_llm))
        console.print(tips())
        while True:
            console.print()
            try:
                line = session.prompt(
                    HTML(f"<rule>{'─' * max(20, console.width)}</rule>\n<prompt>&gt; </prompt>"),
                    bottom_toolbar=toolbar,
                    refresh_interval=0.5,
                )
            except KeyboardInterrupt:
                if time.monotonic() - state["armed"] < EXIT_WINDOW_S:
                    break
                state["armed"] = time.monotonic()
                continue
            except EOFError:  # Ctrl+D / Ctrl+Z, or the input closed
                break
            state["armed"] = 0.0
            try:
                keep_going = repl.handle(line)
            except KeyboardInterrupt:
                console.print(f"[yellow]{terminal.stop_message(repl.site or '')}[/]")
                keep_going = True
            if not keep_going:
                break
    finally:
        terminal.SESSION["active"] = False
        repl.ask, repl.invoke = plain_ask, plain_invoke
    console.print(Text("Goodbye. Your results are in ", style="dim") + file_link(repl.out))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
