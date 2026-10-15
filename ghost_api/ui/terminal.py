"""Terminal interface: renders pipeline events (`ghost_api/events.py`).

Two modes:
  live   For an interactive terminal, in the style of Claude Code: section headers
         (`⏺ Exploring`), compact indented results (`⎿`), a spinner status line that
         updates in place (phase stepper, time, requests, endpoints, LLM usage), and a
         summary card at the end.
  plain  Simple lines, for logs, CI, `--plain`, and when output isn't a terminal.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from ghost_api.events import (
    AgentStep,
    ClientGenerated,
    EndpointsUpdated,
    Event,
    HypothesisTested,
    Notice,
    PhaseChanged,
    ProbeFinished,
    RequestCaptured,
    RunFinished,
    RunStarted,
    UsageUpdated,
    VerifyRound,
)
from ghost_api.ui import keys

ACCENT = "#a78bfa"  # ghost violet, shared with the web dashboard
METHOD_STYLE = {
    "GET": "bold #22c55e",
    "POST": "bold #3b82f6",
    "PUT": "bold #f59e0b",
    "PATCH": "bold #f59e0b",
    "DELETE": "bold #ef4444",
}
STATUS_STYLE = {"verified": "green", "unverified": "yellow", "failed": "red"}
CONFIDENCE_STYLE = {"High": "green", "Medium": "yellow", "Low": "red"}
RESULT_MARK = {
    "confirmed": ("✓", "green"),
    "rejected": ("✗", "red"),
    "inconclusive": ("?", "yellow"),
}
LEVEL_MARK = {
    "info": ("⎿", "dim"),
    "success": ("✓", "green"),
    "warning": ("!", "yellow"),
    "error": ("✗", "red"),
}
PHASE_TITLE = {
    "checks": "Checking the site's rules",
    "explore": "Exploring the site",
    "test": "Testing endpoints with direct calls",
    "generate": "Generating the client",
    "verify": "Verifying against the live API",
    "learn": "Saving what was learned",
    "done": "Done",
}
STEPPER = ("explore", "test", "generate", "verify", "learn")


def method_text(method: str) -> Text:
    return Text(f"{method:<6}", style=METHOD_STYLE.get(method, "bold"))


# Set by the interactive session (ui/repl.py), so hints name its /commands.
SESSION = {"active": False}
# How far the current run got, for the message shown when it is stopped at once.
PROGRESS: dict[str, Any] = {"site": "", "step": 0, "endpoints": 0}


def stop_message(site: str = "") -> str:
    """'Stopped on SITE after step 6. Found 3 endpoints so far; what was captured is saved.'"""
    where = f" on {PROGRESS['site'] or site}" if (PROGRESS["site"] or site) else ""
    step = f" after step {PROGRESS['step']}" if PROGRESS["step"] else ""
    count = PROGRESS["endpoints"]
    found = f"Found {count} endpoint{'s' if count != 1 else ''} so far; " if count else ""
    return f"Stopped{where}{step}. {found}what was captured is saved."


def _next_steps() -> str:
    if SESSION["active"]:
        return "Next: /fetch to download the data · /watch add for alerts · /ui for the dashboard"
    return "Next: ghost-api fetch SITE (download the data) · ghost-api ui (open the dashboard)"


def file_url(path: str | Path) -> str:
    """A local path as a file:/// URL: E:/workspace/.../report.html."""
    return Path(path).resolve().as_uri()


def _hyperlinks_supported() -> bool:
    return bool(
        os.environ.get("WT_SESSION")
        or os.environ.get("TERM_PROGRAM") in {"vscode", "iTerm.app", "WezTerm"}
        or os.environ.get("GHOST_API_SHORT_LINKS")
    )


def file_link(path: str | Path, label: str | None = None) -> Text:
    """A clickable link to a local file (Ctrl+click in Windows Terminal, VS Code...).

    Terminals that understand hyperlinks show a short relative path; others (the classic
    cmd window) show the full file:/// URL, which they can still open."""
    if not str(path):
        return Text("")
    url = file_url(path)
    if label is None and not _hyperlinks_supported():
        label = url
    if label is None:
        try:
            label = str(Path(path).resolve().relative_to(Path.cwd()))
        except ValueError:
            label = str(Path(path).name)
    return Text(label, style=f"link {url} underline")


def endpoint_table(endpoints: list[dict[str, Any]], title: str | None = None) -> Table:
    """A compact endpoint table from `pipeline.endpoint_summary` dicts."""
    table = Table(title=title, box=None, pad_edge=False, show_edge=False, header_style="dim")
    for col in ("", "Endpoint", "Status", "Confidence", "Paging", "Auth", "Returns"):
        table.add_column(col, overflow="fold")
    for ep in endpoints:
        status = ep.get("status") or "discovered"
        paging = "-"
        if ep.get("pagination"):
            p = ep["pagination"]
            paging = f"{p['style']}: {p['param']}" + ("" if p.get("confirmed") else "?")
        path = ep["label"].split(" ", 1)[1] if " " in ep["label"] else ep["label"]
        table.add_row(
            method_text(ep["method"]),
            Text(path, style="bold"),
            Text(status, style=STATUS_STYLE.get(status, "dim")),
            Text(
                ep.get("confidence", "-"), style=CONFIDENCE_STYLE.get(ep.get("confidence", ""), "")
            ),
            paging,
            ep.get("auth_type") or "-",
            Text(ep.get("fields", "-"), style="dim"),
        )
    return table


class TerminalRenderer:
    """Subscribe `renderer.handle` to an EventBus; call `close()` when the run is over."""

    def __init__(self, console: Console | None = None, plain: bool | None = None) -> None:
        self.console = console or Console()
        self.plain = (not self.console.is_terminal) if plain is None else plain
        self.live: Live | None = None
        self.started = time.monotonic()
        self.phase = "checks"
        self.domain = ""
        self.requests = 0
        self.endpoints: list[dict[str, Any]] = []
        self.client: ClientGenerated | None = None
        self.usage: UsageUpdated | None = None
        self.activity = "Starting..."
        self._probe_label = ""

    # --- lifecycle ---

    def start(self) -> None:
        if not self.plain and self.live is None:
            self.live = Live(
                self._status(), console=self.console, refresh_per_second=10, transient=True
            )
            self.live.start()

    def close(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Stop the status line while asking the person something."""
        was_live = self.live is not None
        self.close()
        try:
            yield
        finally:
            if was_live:
                self.start()

    def ask(self, question: str) -> str:
        with self.paused(), keys.input_paused():
            self.console.print(f"\n[bold yellow]The agent asks:[/] {question}")
            return self.console.input("[bold]Your answer[/] (press Enter when done): ")

    # --- events ---

    def handle(self, event: Event) -> None:
        method = getattr(self, f"_on_{event.kind}", None)
        if method is not None:
            method(event)
        if self.live is not None:
            self.live.update(self._status())

    def _print(self, *items: RenderableType | str) -> None:
        target = self.live.console if self.live is not None else self.console
        target.print(*items)

    def _on_run_started(self, e: RunStarted) -> None:
        self.domain = e.domain
        self.started = time.monotonic()
        PROGRESS.update(site=e.domain, step=0, endpoints=0)
        if self.plain:
            self._print(f"[bold]Goal:[/] {e.goal}")
            self._print("[bold]Exploring site...[/]")
            return
        title = "Refreshing" if e.mode == "refresh" else "Exploring"
        self._print(
            Text.assemble(
                ("◆ Ghost API ", f"bold {ACCENT}"), (f"{title} ", "dim"), (e.domain, "bold")
            )
        )
        self._print(Text.assemble(("  goal ", "dim"), e.goal))
        self._print("")

    def _on_phase(self, e: PhaseChanged) -> None:
        self.phase = e.phase
        self.activity = PHASE_TITLE.get(e.phase, e.phase)
        if not self.plain and e.phase not in ("checks", "done"):
            self._print(Text.assemble(("⏺ ", ACCENT), (PHASE_TITLE[e.phase], "bold")))

    def _on_notice(self, e: Notice) -> None:
        if self.plain:
            style = {"success": "green", "warning": "yellow", "error": "red"}.get(e.level)
            prefix = {"success": "OK ", "warning": "! ", "error": ""}.get(e.level, "")
            text = f"[{style}]{prefix}{e.text}[/]" if style else e.text
            self._print(text)
        else:
            mark, style = LEVEL_MARK[e.level]
            self._print(
                Text.assemble(
                    ("  ", ""), (f"{mark} ", style), (e.text, "" if e.level == "info" else style)
                )
            )
        if e.detail:
            self._print(Text(f"    {e.detail}", style="dim"))
        if e.level == "info":
            self.activity = e.text

    def _on_request(self, e: RequestCaptured) -> None:
        self.requests += 1
        if self.plain:
            tag = "third-party " if e.third_party else ""
            self._print(f"      [dim cyan]{tag}{e.method} {e.status} {_short(e.url, 70)}[/]")

    def _on_step(self, e: AgentStep) -> None:
        PROGRESS["step"] = e.step
        args = ", ".join(f"{k}={v!r}" for k, v in e.args.items() if v not in (None, "", False))
        if self.plain:
            self._print(f"[bold]Step {e.step}[/] {e.action}({args})  [dim]{e.thought}[/]")
            for label in e.new_endpoints:
                self._print(f"   [green]+ new endpoint[/] {label}")
            if e.result.startswith(("rejected", "failed")):
                self._print(f"   [yellow]{e.result}[/]")
            return
        self._print(
            Text.assemble(
                ("  ⎿ ", "dim"),
                (f"Step {e.step} ", "bold"),
                (f"{e.action}", ACCENT),
                (f"({args})", "dim"),
            )
        )
        self._print(Text(f"     {e.thought}", style="italic dim"))
        for label in e.new_endpoints:
            method, _, path = label.partition(" ")
            self._print(
                Text.assemble(("     + ", "green"), method_text(method), (path, "bold green"))
            )
        if e.result.startswith(("rejected", "failed")):
            self._print(Text(f"     {e.result}", style="yellow"))
        self.activity = f"Step {e.step}: {e.action}"

    def _on_endpoints(self, e: EndpointsUpdated) -> None:
        self.endpoints = e.endpoints
        PROGRESS["endpoints"] = len(e.endpoints)

    def _on_hypothesis(self, e: HypothesisTested) -> None:
        mark, style = RESULT_MARK[e.result]
        if self.plain:
            if e.endpoint != self._probe_label:
                self._print(f"  [bold]{e.endpoint}[/]")
                self._probe_label = e.endpoint
            self._print(f"    [{style}]{e.result:<12}[/] {e.text}  [dim]{e.evidence}[/]")
            return
        if e.endpoint != self._probe_label:
            method, _, path = e.endpoint.partition(" ")
            self._print(Text.assemble(("  ⎿ ", "dim"), method_text(method), (path, "bold")))
            self._probe_label = e.endpoint
        self._print(Text.assemble((f"     {mark} ", style), e.text, (f"  {e.evidence}", "dim")))

    def _on_probe_done(self, e: ProbeFinished) -> None:
        text = f"{e.direct_calls} direct calls made"
        self._print(f"[dim]{text}[/]" if self.plain else Text(f"  ⎿ {text}", style="dim"))

    def _on_verify_round(self, e: VerifyRound) -> None:
        parts = [
            (f"{e.passed} passed", "green"),
            (f"{e.failed} failed", "red"),
            (f"{e.skipped} skipped", "yellow"),
        ]
        shown = [(t, s) for t, s in parts if not t.startswith("0 ")]
        if self.plain:
            summary = ", ".join(f"[{s}]{t}[/]" for t, s in shown) or "no tests"
            self._print(f"  [bold]Round {e.number}:[/] {summary}")
            for f in e.failures:
                self._print(f"    [red]failed[/] {f['name']}  [dim]{f['message'][:110]}[/]")
            for r in e.repairs:
                self._print(f"    [cyan]repair[/] {r['method']} ({r['kind']}): {r['action']}")
            return
        line = Text.assemble(("  ⎿ ", "dim"), (f"Round {e.number}  ", "bold"))
        for i, (t, s) in enumerate(shown):
            line.append((" · " if i else "") + t, style=s)
        self._print(line)
        for f in e.failures:
            self._print(
                Text.assemble(("     ✗ ", "red"), f["name"], (f"  {f['message'][:100]}", "dim"))
            )
        for r in e.repairs:
            self._print(
                Text.assemble(
                    ("     ↻ ", "cyan"),
                    (f"{r['method']} ", "bold"),
                    (f"({r['kind']}) ", "dim"),
                    r["action"],
                )
            )

    def _on_client(self, e: ClientGenerated) -> None:
        self.client = e

    def _on_usage(self, e: UsageUpdated) -> None:
        self.usage = e

    def _on_run_finished(self, e: RunFinished) -> None:
        self.close()
        if self.plain:
            self._plain_summary(e)
        else:
            self.console.print()
            self.console.print(self._summary_card(e))

    # --- the live status line ---

    def _status(self) -> RenderableType:
        elapsed = time.monotonic() - self.started
        stepper = Text("  ")
        current = STEPPER.index(self.phase) if self.phase in STEPPER else -1
        if self.phase in ("done",):
            current = len(STEPPER)
        for i, phase in enumerate(STEPPER):
            if i < current:
                stepper.append(f"✓ {phase.title()}  ", style="green")
            elif i == current:
                stepper.append(f"◐ {phase.title()}  ", style=f"bold {ACCENT}")
            else:
                stepper.append(f"○ {phase.title()}  ", style="dim")
        stats = Text(
            f"{elapsed:4.0f}s · {_count(self.requests, 'request')} · "
            f"{_count(len(self.endpoints), 'endpoint')}",
            style="dim",
        )
        if self.usage is not None:
            stats.append(
                f" · LLM {self.usage.answered} calls · {self.usage.tokens / 1000:.1f}k tok",
                style="dim",
            )
        spinner = Spinner("dots", text=Text(self.activity[:90], style=ACCENT), style=ACCENT)
        return Group(Text(""), spinner, stepper, Text.assemble(("  ", ""), stats))

    # --- the end of a run ---

    def _summary_card(self, e: RunFinished) -> Panel:
        ok = e.status in ("goal-covered", "finished", "captured", "refreshed")
        title = Text.assemble(
            ("✓ " if ok else "! ", "green" if ok else "yellow"),
            (f"{e.status}", "bold"),
            (f"  ·  {e.seconds:.0f}s", "dim"),
        )
        body: list[RenderableType] = [Text(e.reason, style="dim"), Text("")]
        if self.endpoints:
            body.append(endpoint_table(self.endpoints))
        else:
            body.append(
                Text(
                    "No JSON API was found. The site may build its pages on the server.",
                    style="yellow",
                )
            )
        if self.client is not None:
            c = self.client
            body += [
                Text(""),
                Text.assemble(
                    ("Client  ", "dim"), (c.class_name, "bold"), (f"  ({c.lang})", "dim")
                ),
            ]
            for m in c.methods:
                sig = f"{m['name']}({', '.join(m['args'])}) -> {m['returns']}"
                extra = f"   + {m['iterator']}()" if m.get("iterator") else ""
                body.append(Text.assemble(("  ", ""), (sig, ACCENT), (extra, "dim")))
            for cred in c.credentials:
                body.append(Text(f"  needs {cred['arg']}: set {cred['env']}", style="yellow"))
            body += [Text(""), Text("Files   ", style="dim") + file_link(c.directory)]
            body.append(Text("Use it  ", style="dim") + Text(_use_it(c), style="bold"))
        elif e.generation_error:
            body += [Text(""), Text(f"No client generated: {e.generation_error}", style="yellow")]
        if e.exposure:
            body += [
                Text(""),
                Text(f"⚠ {e.exposure}", style="bold yellow"),
                Text(
                    '  Ghost API hid these values. See the report\'s "Personal data" section.',
                    style="dim",
                ),  # fmt: skip
            ]
        body.append(Text("Report  ", style="dim") + file_link(e.report_html or e.report))
        if e.report_html:
            body.append(
                Text("        Ctrl+click to open it in your browser, or /open", style="dim")
            )
        if e.lessons_learned:
            body.append(
                Text(f"Remembered {e.lessons_learned} lesson(s) for next time", style="dim")
            )
        if self.usage is not None:
            u = self.usage
            body.append(
                Text(
                    f"LLM     {u.answered} calls · {u.tokens:,} tokens · {u.cache_hits} cached",
                    style="dim",
                )
            )
        body += [
            Text(""),
            Text(_next_steps(), style=f"dim {ACCENT}"),
        ]
        return Panel(
            Group(*body), title=title, title_align="left", border_style=ACCENT, padding=(1, 2)
        )

    def _plain_summary(self, e: RunFinished) -> None:
        self.console.print()
        self.console.print(f"[bold]Finished:[/] {e.status} ({e.reason})")
        if self.endpoints:
            self.console.print(endpoint_table(self.endpoints, f"Endpoints ({len(self.endpoints)})"))
        else:
            self.console.print(
                "[yellow]No JSON API requests captured.[/] The site may render on the server."
            )
        if e.filtered:
            noise = ", ".join(
                f"{k}: {v}" for k, v in sorted(e.filtered.items(), key=lambda x: -x[1])
            )
            self.console.print(f"[dim]Filtered out: {noise}[/]")
        self.console.print(
            f"[dim]{e.requests} requests -> {e.endpoints} endpoint"
            f"{'' if e.endpoints == 1 else 's'} in {e.seconds:.1f}s[/]"
        )
        if self.client is not None:
            print_client(self.console, self.client)
        if e.lessons_learned:
            self.console.print(
                f"[dim]Remembered {e.lessons_learned} new lesson(s) for next time[/]"
            )
        self.console.print(Text("Report: ", style="dim") + file_link(e.report_html or e.report))
        if self.usage is not None:
            u = self.usage
            providers = ", ".join(f"{k}: {v}" for k, v in u.by_provider.items()) or "-"
            self.console.print(
                f"[dim]LLM: {u.answered} answered calls ({providers}), {u.tokens:,} tokens, "
                f"{u.cache_hits} cached, {u.retries} retries, {u.fallbacks} fallbacks[/]"
            )
            for problem in u.problems:
                self.console.print(f"[dim]   {problem}[/]")


def print_client(console: Console, c: ClientGenerated) -> None:
    how = "names chosen by the LLM" if c.named_by_llm else "names derived from URLs"
    console.print(f"\n[bold green]Generated[/] {c.class_name} ({how}) in {c.directory}")
    for m in c.methods:
        extra = f"  + {m['iterator']}()" if m.get("iterator") else ""
        console.print(f"   {m['name']}({', '.join(m['args'])}) -> {m['returns']}{extra}")
    for cred in c.credentials:
        console.print(
            f"   [yellow]needs[/] {cred['arg']} (set {cred['env']} or pass it to the client)"
        )
    for skipped in c.skipped:
        console.print(f"   [dim]skipped {skipped}[/]")
    console.print(f"[dim]Files: {', '.join(c.files)}[/]")


def _use_it(c: ClientGenerated) -> str:
    if not c.methods:
        return ""
    module = c.files[0].rsplit(".", 1)[0] if c.files else "client"
    first = c.methods[0]
    if c.lang == "typescript":
        call = f"await new {c.class_name}().{first['name']}(...)"
        return f'import {{ {c.class_name} }} from "./{module}.ts"; {call}'
    return f"from {module} import {c.class_name}; {c.class_name}().{first['name']}(...)"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _short(url: str, width: int = 80) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    text = parts.path + (f"?{parts.query}" if parts.query else "")
    return text if len(text) <= width else text[: width - 3] + "..."
