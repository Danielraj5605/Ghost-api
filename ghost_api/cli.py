"""Command-line entry point: `ghost-api [run | refresh | generate | verify | report | inspect |
fetch | watch | serve | mcp | doctor | ui]`. With no command, asks for a URL and goal interactively.

The workflow itself lives in `ghost_api/pipeline.py` and emits events; this module parses
options and renders those events with `ghost_api/ui/terminal.py`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ghost_api import __version__, exitcodes, watch
from ghost_api.config import (
    Settings,
    check_language,
    default_db_path,
    load_env,
    state_dir,
)
from ghost_api.events import (
    ClientGenerated,
    EndpointsUpdated,
    EventBus,
    Notice,
    RunFinished,
    RunStarted,
)
from ghost_api.export import tabular, write_exports
from ghost_api.export.site_data import endpoint_summary
from ghost_api.export.tabular import FORMATS
from ghost_api.generator import NothingToGenerate, generate_client
from ghost_api.generator.python_gen import GenerationError
from ghost_api.llm.doctor import check_providers
from ghost_api.llm.gateway import LLMGateway
from ghost_api.llm.providers import default_providers
from ghost_api.memory.knowledge import load_memory, memory_prompt
from ghost_api.memory.store import RunRow, Store, StoreError
from ghost_api.net import RateLimiter
from ghost_api.pipeline import (
    RunOptions,
    client_event,
    explore_site,
    finish,
    open_session,
    refresh_site,
    robots_policy,
    round_event,
)
from ghost_api.probe.replay import ReplayRefused
from ghost_api.project import ProjectError, default_map, find_project
from ghost_api.runtime import OperationError, SiteRuntime
from ghost_api.site import SiteAmbiguous, resolve_site, site_host
from ghost_api.ui import keys
from ghost_api.ui.terminal import (
    ACCENT,
    TerminalRenderer,
    endpoint_table,
    file_link,
    print_client,
)
from ghost_api.verify.verifier import Verifier

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
    from ghost_api.ui import keys
    from ghost_api.ui.repl import Repl, run_session

    def ask(question: str) -> str:
        with keys.input_paused():
            return console.input(f"[bold]{question}[/]")

    def has_llm() -> bool:
        load_env()  # a key added to .env while the session is open counts at once
        return any(p.api_key for p in default_providers())

    repl = Repl(console, _invoke, ask)
    run_session(console, repl, has_llm, history_file=state_dir() / "history.txt")


class _RunResult:
    """Collects a run's outcome from its events, for `--json`."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"status": "failed", "endpoints": [], "client": None}

    def __call__(self, event: Any) -> None:
        if isinstance(event, RunStarted):
            self.data.update(run_id=event.run_id, site=event.domain, url=event.start_url)
        elif isinstance(event, EndpointsUpdated):
            self.data["endpoints"] = event.endpoints
        elif isinstance(event, ClientGenerated):
            self.data["client"] = event.to_dict()
        elif isinstance(event, RunFinished):
            finished = event.to_dict()
            finished.pop("type", None)
            finished["endpoint_count"] = finished.pop("endpoints")  # the list stays in "endpoints"
            self.data.update(finished)

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


def _gateway(max_calls: int, required: bool) -> LLMGateway | None:
    gateway = LLMGateway(
        default_providers(), max_calls=max_calls, cooldown_file=state_dir() / "cooldowns.json"
    )
    if gateway.providers:
        return gateway
    if required:
        console.print(
            "[red]No LLM API key found.[/] Add one to .env (see .env.example), "
            "or use --no-agent to only scroll the page."
        )
        raise typer.Exit(code=2)
    return None


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


@app.command()
def run(
    url: Annotated[str, typer.Argument(help="Website to explore, e.g. https://example.com")],
    goal: Annotated[str, typer.Option("--goal", "-g", help="What the client should do.")],
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    headed: Annotated[bool, typer.Option("--headed", help="Show the browser window.")] = False,
    max_steps: Annotated[int, typer.Option(help="Maximum agent actions.")] = 40,
    max_llm_calls: Annotated[int, typer.Option(help="Maximum LLM calls.")] = 50,
    max_minutes: Annotated[float, typer.Option(help="Time limit in minutes.")] = 15.0,
    rate_limit: Annotated[
        float, typer.Option(help="Max requests per second the tool sends itself.")
    ] = 1.0,
    ignore_robots: Annotated[
        bool, typer.Option("--ignore-robots", help="Skip the robots.txt check.")
    ] = False,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    no_agent: Annotated[
        bool, typer.Option("--no-agent", help="Don't use the LLM; just load and scroll the page.")
    ] = False,
    no_probe: Annotated[
        bool, typer.Option("--no-probe", help="Don't test endpoints by calling them directly.")
    ] = False,
    max_probes: Annotated[
        int, typer.Option(help="Maximum direct API calls for testing endpoints.")
    ] = 60,
    no_generate: Annotated[
        bool, typer.Option("--no-generate", help="Don't generate a client.")
    ] = False,
    no_verify: Annotated[
        bool, typer.Option("--no-verify", help="Don't run the generated tests against the API.")
    ] = False,
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
) -> None:
    """Explore a website with the AI agent and map its hidden API."""
    console.print(f"[dim]⏳ Starting run on {url}...[/]")
    try:
        settings = Settings(
            url=_normalize_url(url),
            goal=goal,
            lang=lang,
            headed=headed,
            max_steps=max_steps,
            max_llm_calls=max_llm_calls,
            max_minutes=max_minutes,
            rate_limit=rate_limit,
            ignore_robots=ignore_robots,
            db_path=db or default_db_path(),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    opts = RunOptions(
        lang=settings.lang,
        headed=settings.headed,
        max_steps=settings.max_steps,
        max_minutes=settings.max_minutes,
        max_probes=max_probes,
        no_probe=no_probe,
        no_generate=no_generate,
        no_verify=no_verify,
        out=out,
    )
    host = urlsplit(settings.url).hostname or ""
    limiter = RateLimiter(settings.rate_limit)
    bus = EventBus()
    renderer = _renderer()
    bus.subscribe(renderer.handle)
    result = _RunResult()
    bus.subscribe(result)

    robots, allowed = robots_policy(settings.url, settings.ignore_robots, limiter, bus)
    if not allowed:
        _not_allowed(settings.url)
    gateway = None if no_agent else _gateway(settings.max_llm_calls, required=True)

    status = "failed"
    with Store(settings.db_path) as store:
        renderer.start()
        try:
            s = open_session(
                store,
                bus,
                host,
                settings.url,
                settings.goal,
                settings.headed,
                limiter,
                robots,
                gateway,
                max_probes,
                screens_root=settings.db_path,
                lang=settings.lang,
            )
            s.stop = s.stop or keys.stop_requested  # Ctrl+C in the session
            known = load_memory(store, s.site)
            if known is not None:
                s.memory = memory_prompt(known.endpoints, known.lessons)
                bus.emit(
                    Notice(
                        f"Using memory: {len(known.endpoints)} known endpoints, "
                        f"{len(known.lessons)} lessons"
                    )
                )
            ask = renderer.ask if settings.headed and sys.stdin.isatty() else None
            started = time.monotonic()
            status, reason = explore_site(s, opts, use_agent=gateway is not None, ask_user=ask)
            finish(s, status, reason, started, opts)
        finally:
            renderer.close()
        console.print(f"[dim]Saved to {settings.db_path}[/]")
    result.finish(settings.db_path)


@app.command("import")
def import_har(
    har: Annotated[Path, typer.Argument(help="A .har file saved from the browser's DevTools.")],
    goal: Annotated[
        str, typer.Option("--goal", "-g", help="What you were doing (for the report).")
    ] = "imported from a HAR file",
    url: Annotated[
        str | None, typer.Option(help="The site's start URL (default: the first page in the file).")
    ] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline", help="Send nothing to the site: no endpoint tests, no client tests."
        ),
    ] = False,
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    rate_limit: Annotated[
        float, typer.Option(help="Max requests per second the tool sends itself.")
    ] = 1.0,
    max_probes: Annotated[
        int, typer.Option(help="Maximum direct API calls for testing endpoints.")
    ] = 60,
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Map a site's API from a HAR file you recorded in your browser (no AI, no browser).

    For sites behind a login or a bot check: browse them yourself with DevTools open
    (Network tab), "Save all as HAR", then import. Cookies, tokens and personal data are
    removed before anything is stored. Delete the .har file afterwards: it holds them.
    """
    from ghost_api.capture.har import HarError, read_har, summary
    from ghost_api.net import RobotsPolicy

    lang = _check_lang(lang)
    try:
        recording = read_har(har, _normalize_url(url) if url else None)
    except (HarError, OSError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=exitcodes.USAGE) from exc
    for line in summary(recording):
        console.print(f"[dim]{line}[/]")
    path = db or default_db_path()
    bus = EventBus()
    renderer = _renderer()
    bus.subscribe(renderer.handle)
    result = _RunResult()
    bus.subscribe(result)
    limiter = RateLimiter(rate_limit)
    host = recording.target_host
    if offline:
        robots = RobotsPolicy.allow_all(host, "offline import: nothing is sent")
    else:
        robots, allowed = robots_policy(recording.start_url, False, limiter, bus)
        if not allowed:  # the recording is still mapped, just never replayed
            offline = True
            bus.emit(Notice("Importing without testing endpoints (robots.txt)", "warning"))
    opts = RunOptions(lang=lang, max_probes=max_probes, no_probe=offline, no_verify=offline,
                      out=out)  # fmt: skip
    with Store(path) as store:
        renderer.start()
        try:
            s = open_session(
                store, bus, host, recording.start_url, goal, False, limiter, robots, None,
                max_probes, screens_root=path, lang=lang,
            )  # fmt: skip
            started = time.monotonic()
            s.phase("explore")
            for captured, raw in recording.captured:
                s.recorder.add(captured, raw)
            s.recorder.dropped.update(recording.dropped)
            reason = f"imported {len(recording.captured)} API calls from {har.name}"
            finish(s, "captured", reason, started, opts)
        finally:
            renderer.close()
        console.print(f"[dim]Saved to {path}. You can delete {har.name} now.[/]")
    result.finish(path)


@app.command()
def refresh(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    headed: Annotated[bool, typer.Option("--headed", help="Show the browser window.")] = False,
    max_steps: Annotated[int, typer.Option(help="Agent steps, if the agent is needed.")] = 10,
    max_llm_calls: Annotated[int, typer.Option(help="Maximum LLM calls.")] = 20,
    rate_limit: Annotated[
        float, typer.Option(help="Max requests per second the tool sends itself.")
    ] = 1.0,
    ignore_robots: Annotated[
        bool, typer.Option("--ignore-robots", help="Skip the robots.txt check.")
    ] = False,
    max_probes: Annotated[
        int, typer.Option(help="Maximum direct API calls for testing endpoints.")
    ] = 60,
    no_verify: Annotated[
        bool, typer.Option("--no-verify", help="Don't run the generated tests against the API.")
    ] = False,
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Never call the LLM (no agent, URL-based names).")
    ] = False,
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Re-check a known site using memory: replay first, browse only if needed.

    Also continues an interrupted run, since its traffic and endpoints were saved.
    """
    console.print(f"[dim]⏳ Starting refresh for {domain}...[/]")
    path = _need_db(db)
    lang = _check_lang(lang)
    with Store(path) as store:
        domain = _site(store, domain)
        mem = load_memory(store, domain)
        if mem is None:
            console.print(f"Nothing remembered for {domain}. Run `ghost-api run` first.")
            raise typer.Exit(code=1)
        bus = EventBus()
        renderer = _renderer()
        bus.subscribe(renderer.handle)
        result = _RunResult()
        bus.subscribe(result)
        bus.emit(
            Notice(
                f"Refreshing {domain} from memory: {len(mem.endpoints)} endpoint(s), "
                f"{len(mem.lessons)} lesson(s)"
            )
        )
        limiter = RateLimiter(rate_limit)
        robots, allowed = robots_policy(mem.start_url, ignore_robots, limiter, bus)
        if not allowed:
            _not_allowed(mem.start_url)
        gateway = None if no_llm else _gateway(max_llm_calls, required=False)
        opts = RunOptions(
            lang=lang,
            headed=headed,
            max_steps=max_steps,
            max_probes=max_probes,
            no_verify=no_verify,
            out=out,
        )
        renderer.start()
        try:
            s = open_session(
                store,
                bus,
                site_host(domain),
                mem.start_url,
                f"refresh: {mem.goal}",
                headed,
                limiter,
                robots,
                gateway,
                max_probes,
                screens_root=path,
                mode="refresh",
                lang=lang,
                site=domain,
            )
            s.stop = s.stop or keys.stop_requested  # Ctrl+C in the session
            s.memory = memory_prompt(mem.endpoints, mem.lessons)
            started = time.monotonic()
            status, reason = refresh_site(s, mem, max_steps)
            finish(s, status, reason, started, opts)
        finally:
            renderer.close()
        first = store.first_run_llm_calls(domain)
        now = gateway.usage.answered if gateway is not None else 0
        if first:
            saved = 100 * (first - now) / first
            console.print(
                f"[bold]Memory:[/] {now} LLM call(s) vs {first} on the first run "
                f"({saved:.0f}% fewer)"
            )
        result.data["llm_calls"] = now
    result.finish(path)


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="Port for the local dashboard.")] = 8800,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Don't open the browser automatically.")
    ] = False,
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    no_watch: Annotated[
        bool, typer.Option("--no-watch", help="Don't check due watches while open.")
    ] = False,
) -> None:
    """Open the web dashboard: start runs, watch the agent live, browse every result."""
    from ghost_api.web.server import serve

    serve(
        db or default_db_path(),
        out,
        port=port,
        open_browser=not no_open,
        console=console,
        run_watches=not no_watch,
    )


@app.command()
def generate(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Use names derived from URLs (no LLM call).")
    ] = False,
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    allow: Annotated[
        list[str] | None,
        typer.Option(
            "--allow",
            help='An endpoint you checked only reads data, e.g. "POST /api/search". Ghost API '
            "skips POSTs without a read word in their URL; this includes one anyway. Repeatable.",
        ),
    ] = None,
) -> None:
    """(Re)generate the client for a site from its stored endpoint map."""
    path = _need_db(db)
    gateway = (
        None
        if no_llm
        else LLMGateway(
            default_providers(), max_calls=3, cooldown_file=state_dir() / "cooldowns.json"
        )
    )
    if gateway is not None and not gateway.providers:
        gateway = None
    try:
        with Store(path) as store:
            domain = _site(store, domain)
            if gateway is not None:
                gateway.store = store
            result = generate_client(
                store, domain, out, llm=gateway, lang=_check_lang(lang), allow=set(allow or [])
            )
    except (NothingToGenerate, GenerationError) as exc:
        console.print(f"[red]No client generated:[/] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        if gateway is not None:
            gateway.close()
    event = client_event(result)
    print_client(console, event)
    if _options["json"]:
        json_out({"site": domain, "client": event.to_dict()})


@app.command()
def verify(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Regenerate a site's client, run its tests against the live API, and repair.

    Uses stored data only. Endpoints that need a key are tested when the key is set in
    the environment (the generated client says which variable).
    """
    console.print(f"[dim]⏳ Starting verification for {domain}...[/]")
    started = time.monotonic()
    path = _need_db(db)
    renderer = TerminalRenderer(console, plain=True)
    with Store(path) as store:
        domain = _site(store, domain)
        run_id = store.latest_endpoint_run(domain)
        run_row = store.latest_run(domain)
        if run_id is None or run_row is None:
            console.print(f"No endpoints stored for {domain}. Run `ghost-api run` first.")
            raise typer.Exit(code=1)
        endpoints = store.current_endpoints(domain)
        console.print(f"[bold]Verifying the {domain} client against the live API...[/]")
        verifier = Verifier(
            domain,
            run_row.start_url,
            endpoints,
            store.requests_for_domain(domain),
            out,
            on_round=lambda r: renderer.handle(round_event(r)),
            lang=_check_lang(lang),
        )
        try:
            report = verifier.run()
        except (NothingToGenerate, GenerationError) as exc:
            console.print(f"[red]No client generated:[/] {exc}")
            raise typer.Exit(code=1) from exc
        store.save_endpoints(domain, run_id, verifier.endpoints)
        for ep in verifier.endpoints:
            state, note = report.statuses.get(ep.key, ("unverified", ""))
            ep.status, ep.status_note = state, note
            store.set_endpoint_status(domain, ep, state, note)
    summaries = [endpoint_summary(e, domain) for e in verifier.endpoints]
    console.print(endpoint_table(summaries, f"Endpoints ({len(summaries)})"))
    if report.result is not None:
        print_client(console, client_event(report.result))
    elapsed = time.monotonic() - started
    console.print(f"[dim]Verification completed in {elapsed:.1f}s[/]")
    failed = any(state == "failed" for state, _ in report.statuses.values())
    if _options["json"]:
        client = client_event(report.result).to_dict() if report.result is not None else None
        json_out(
            {"site": domain, "passed": not failed, "seconds": round(elapsed, 1),
             "endpoints": summaries, "client": client}
        )  # fmt: skip
    if failed:
        raise typer.Exit(code=exitcodes.FAILED)


@app.command()
def report(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    run_id: Annotated[
        int | None, typer.Option("--run", help="Run to report on (default: the latest).")
    ] = None,
    out: Annotated[Path, typer.Option(help="Folder for generated files.")] = Path("output"),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Write report.md, report.html and openapi.yaml for a site from what is stored."""
    path = _need_db(db)
    with Store(path) as store:
        domain = _site(store, domain)
        run_row = store.get_run(run_id) if run_id else store.latest_run(domain)
        if run_row is None or run_row.domain != domain:
            console.print(f"No run found for {domain}.")
            raise typer.Exit(code=1)
        exports = write_exports(store, domain, run_row.id, out)
    console.print(Text("Report:  ") + file_link(exports.report))
    if exports.html:
        console.print(Text("Report (shareable page): ") + file_link(exports.html))
    console.print(f"OpenAPI: {exports.openapi}")
    if _options["json"]:
        json_out(
            {"site": domain, "run_id": run_row.id, "report": str(exports.report),
             "report_html": str(exports.html or ""), "openapi": str(exports.openapi)}
        )  # fmt: skip


def _params(pairs: list[str]) -> dict[str, str]:
    """['area=velachery', 'sport=football'] -> {'area': 'velachery', 'sport': 'football'}."""
    params = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            console.print(f"Inputs look like name=value, not {pair!r}.")
            raise typer.Exit(code=2)
        params[name.strip()] = value
    return params


def _operation_json(op: Any) -> dict[str, Any]:
    return {
        "name": op.name,
        "summary": op.summary,
        "endpoint": op.label,
        "inputs": [asdict(i) for i in op.inputs],
        "pagination": op.pagination,
    }


def _operations_table(rt: SiteRuntime) -> Table:
    table = Table(title=f"What you can fetch from {rt.site}", title_justify="left")
    for col in ("Operation", "What it returns", "Inputs (example)", "Pages"):
        table.add_column(col, overflow="fold")
    for op in rt.operations():
        inputs = ", ".join(f"{i.name}={i.example}" if i.example else i.name for i in op.inputs)
        pages = op.pagination["style"] if op.pagination else "-"
        table.add_row(f"[bold {ACCENT}]{op.name}[/]", op.summary, inputs or "-", pages)
    return table


@app.command()
def fetch(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    operation: Annotated[
        str | None, typer.Argument(help="What to fetch; leave out to list the choices.")
    ] = None,
    inputs: Annotated[
        list[str] | None, typer.Argument(help="Inputs as name=value, e.g. area=velachery.")
    ] = None,
    fmt: Annotated[str, typer.Option("--format", help="csv, xlsx or json.")] = "csv",
    pages: Annotated[int, typer.Option(help="Maximum pages to follow.")] = 5,
    max_rows: Annotated[int, typer.Option(help="Stop after this many rows.")] = 1000,
    save_to: Annotated[
        Path | None, typer.Option("--save-to", help="File to write (default: in output/).")
    ] = None,
    include_sensitive: Annotated[
        bool,
        typer.Option(
            "--include-sensitive",
            help="Keep personal data (emails, phones, IDs). Only for auditing your own site.",
        ),
    ] = False,
    out: Annotated[Path, typer.Option(help="Folder for generated files.")] = Path("output"),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Download a site's data as CSV, Excel or JSON, following all pages. No code needed."""
    console.print(f"[dim]⏳ Fetching data from {domain}...[/]")
    started = time.monotonic()
    if fmt not in FORMATS:
        console.print(f"--format must be one of: {', '.join(FORMATS)}")
        raise typer.Exit(code=2)
    path = _need_db(db)
    with Store(path) as store:
        domain = _site(store, domain)
        try:
            rt = SiteRuntime(store, domain, include_sensitive=include_sensitive)
        except OperationError as exc:
            console.print(str(exc))
            raise typer.Exit(code=1) from exc
    with rt:
        if operation is None and _options["json"]:
            json_out({"site": domain, "operations": [_operation_json(o) for o in rt.operations()]})
            return
        if operation is None:
            console.print(_operations_table(rt))
            ops = rt.operations()
            if ops:
                console.print(f"[dim]Example: ghost-api fetch {domain} {ops[0].name}[/]")
            else:
                console.print(
                    "[yellow]No fetchable endpoints found.[/] This site's endpoints may use "
                    "authentication or change data, so Ghost API won't call them directly.\n"
                    "[dim]If you know an endpoint only reads data, re-generate with:\n"
                    f'  ghost-api generate {domain} --allow "POST /api/search"[/]'
                )
            return
        try:
            op = rt.operation(operation)
            records: list[object] = []
            hidden = 0
            for page in rt.iterate(op, _params(inputs or []), pages, max_rows):
                if not page.result.ok:
                    detail = page.result.error or f"HTTP {page.result.status}"
                    console.print(f"[yellow]Page {page.number} failed:[/] {detail}")
                    break
                records += page.records
                hidden += sum(int(h.get("count", 1)) for h in page.result.hidden)
                console.print(f"  Page {page.number}: {len(page.records)} rows")
        except (OperationError, ReplayRefused) as exc:
            console.print(str(exc))
            raise typer.Exit(code=1) from exc
    if not records:
        console.print("No rows came back. Try other inputs (see `ghost-api fetch SITE`).")
        raise typer.Exit(code=1)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = save_to or out / domain / "data" / f"{op.name}-{stamp}.{fmt}"
    tabular.write(records, target, fmt)
    elapsed = time.monotonic() - started
    saved = Text(f"Saved {len(records)} rows in {elapsed:.1f}s: ", style="bold green")
    console.print(saved + file_link(target))
    if hidden:
        console.print(f"[dim]{hidden} personal values hidden[/]")
    if _options["json"]:
        json_out(
            {"site": domain, "operation": op.name, "file": str(target), "rows": len(records),
             "personal_values_hidden": hidden, "records": records}
        )  # fmt: skip


watch_app = typer.Typer(
    help="Watch a site for new, changed or removed items, and get alerts.",
    no_args_is_help=True,
)
app.add_typer(watch_app, name="watch")
TASK_NAME = "GhostAPI watch"


def _print_check(result: watch.CheckResult) -> None:
    style = "red" if result.error else ("green" if result.changes else "dim")
    console.print(f"[{style}]#{result.watch['id']} {result.headline()}[/]")
    for change in result.changes[: watch.MAX_CHANGES_LISTED]:
        console.print(f"    {change.line()}", markup=False)
    if len(result.changes) > watch.MAX_CHANGES_LISTED:
        console.print(f"    ... and {len(result.changes) - watch.MAX_CHANGES_LISTED} more")
    for note in result.notified:
        console.print(f"    [dim]notify {note}[/]")


@watch_app.command("add")
def watch_add(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    operation: Annotated[str, typer.Argument(help="What to watch (see `ghost-api fetch SITE`).")],
    inputs: Annotated[
        list[str] | None, typer.Argument(help="Inputs as name=value, e.g. area=velachery.")
    ] = None,
    every: Annotated[str, typer.Option(help="How often to check: 30m, 6h, 1d...")] = "6h",
    notify: Annotated[
        list[str] | None,
        typer.Option(help="ntfy:TOPIC, webhook:URL or desktop. Repeatable."),
    ] = None,
    track: Annotated[
        str | None,
        typer.Option(help="Only these fields count as changes, e.g. price,name (default: all)."),
    ] = None,
    pages: Annotated[int, typer.Option(help="Pages to read on each check.")] = 5,
    no_check: Annotated[
        bool, typer.Option("--no-check", help="Don't take the first snapshot now.")
    ] = False,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Start watching an endpoint. The first check records what's there now."""
    try:
        minutes = watch.parse_every(every)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(code=2) from exc
    path = _need_db(db)
    with Store(path) as store:
        domain = _site(store, domain)
        try:
            with SiteRuntime(store, domain) as rt:
                op = rt.operation(operation)
        except OperationError as exc:
            console.print(str(exc))
            raise typer.Exit(code=1) from exc
        watch_id = store.add_watch(
            {
                "site": domain,
                "operation": op.name,
                "params": _params(inputs or []),
                "every_minutes": minutes,
                "notify": notify or [],
                "track": [t.strip() for t in (track or "").split(",") if t.strip()],
                "max_pages": pages,
            }
        )
        console.print(
            f"Watching #{watch_id}: {domain} {op.name}, every {watch.every_label(minutes)}."
        )
        if not no_check:
            w = store.get_watch(watch_id)
            assert w is not None
            _print_check(watch.check(store, w))
    console.print(
        "[dim]Checks run while `ghost-api watch start` or `ghost-api ui` is open, or in the "
        "background after `ghost-api watch schedule`.[/]"
    )


@watch_app.command("list")
def watch_list(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Show your watches."""
    with Store(_need_db(db)) as store:
        rows = store.watches()
    if _options["json"]:
        json_out({"watches": rows})
        return
    if not rows:
        console.print("No watches yet. Add one with `ghost-api watch add SITE OPERATION`.")
        return
    table = Table(title="Watches", title_justify="left")
    for col in ("ID", "Watching", "Every", "Notify", "Last check", "Result"):
        table.add_column(col, overflow="fold")
    for w in rows:
        table.add_row(
            str(w["id"]), watch.describe(w), watch.every_label(w["every_minutes"]),
            ", ".join(w["notify"]) or "-", (w["last_check_at"] or "never").replace("T", " "),
            w["last_status"] or "-",
        )  # fmt: skip
    console.print(table)


@watch_app.command("run")
def watch_run(
    ids: Annotated[list[int] | None, typer.Argument(help="Watch IDs (default: all).")] = None,
    due: Annotated[bool, typer.Option("--due", help="Only watches that are due.")] = False,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Check watches once now."""
    with Store(_need_db(db)) as store:
        targets = [w for w in store.watches() if not ids or w["id"] in ids]
        if due:
            targets = [w for w in targets if watch.is_due(w)]
        if not targets:
            console.print("Nothing to check." if due else "No such watch.")
            return
        for w in targets:
            _print_check(watch.check(store, w))


@watch_app.command("changes")
def watch_changes(
    watch_id: Annotated[int | None, typer.Argument(help="One watch (default: all).")] = None,
    limit: Annotated[int, typer.Option(help="How many to show.")] = 30,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Show recent changes found by your watches."""
    with Store(_need_db(db)) as store:
        changes = store.watch_changes(watch_id, limit)
    if not changes:
        console.print("No changes recorded yet.")
        return
    for c in changes:
        line = watch.Change(c["kind"], c["key"], c["title"], c.get("fields") or {}).line()
        console.print(f"[dim]{c['at'].replace('T', ' ')}  #{c['watch_id']}[/]  ", end="")
        console.print(line, markup=False)


@watch_app.command("remove")
def watch_remove(
    watch_id: Annotated[int, typer.Argument(help="The watch to remove.")],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Stop watching (removes its history too)."""
    with Store(_need_db(db)) as store:
        removed = store.remove_watch(watch_id)
    console.print(f"Removed watch #{watch_id}." if removed else f"No watch #{watch_id}.")
    if not removed:
        raise typer.Exit(code=1)


@watch_app.command("start")
def watch_start(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Keep checking due watches until you press Ctrl+C."""
    with Store(_need_db(db)) as store:
        if not store.watches():
            console.print(
                "No watches yet, so there is nothing to check. Add one first, e.g.\n"
                "  ghost-api watch add quotes.toscrape.com get_quotes --every 6h"
            )
            raise typer.Exit(code=1)
        console.print("Checking due watches every minute. Press Ctrl+C to stop.")
        try:
            watch.loop(store, _print_check)
        except KeyboardInterrupt:
            console.print("Stopped.")


def schedule_command(db: Path, every_minutes: int) -> list[str]:
    """The Windows Task Scheduler command that runs due watches in the background."""
    launcher = Path.home() / ".local" / "bin" / "ghost-api.cmd"
    run = (
        f'"{launcher}"' if launcher.exists() else f'"{Path(sys.executable)}" -m ghost_api'
    ) + f' watch run --due --db "{db.resolve()}"'
    minutes = max(5, min(every_minutes, 1439))
    return ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(minutes),
            "/TN", TASK_NAME, "/TR", run]  # fmt: skip


@watch_app.command("schedule")
def watch_schedule(
    every: Annotated[str, typer.Option(help="How often Windows wakes Ghost API up.")] = "30m",
    remove: Annotated[bool, typer.Option("--remove", help="Remove the scheduled task.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the command without running it.")
    ] = False,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Run due watches in the background (Windows Task Scheduler), even with no window open."""
    import subprocess

    path = _need_db(db)
    if sys.platform != "win32":
        where = path.resolve()
        line = f'*/30 * * * * cd "{Path.cwd()}" && ghost-api watch run --due --db "{where}"'
        console.print("Add this line with `crontab -e` (checks every 30 minutes):")
        console.print(line, markup=False)
        return
    if remove:
        command = ["schtasks", "/Delete", "/F", "/TN", TASK_NAME]
    else:
        try:
            command = schedule_command(path, watch.parse_every(every))
        except ValueError as exc:
            console.print(str(exc))
            raise typer.Exit(code=2) from exc
    if dry_run:
        console.print(subprocess.list2cmdline(command), markup=False)
        return
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        console.print(f"Task Scheduler said: {(done.stderr or done.stdout).strip()}")
        raise typer.Exit(code=1)
    console.print(
        "Removed the background task."
        if remove
        else f'Windows will check due watches every {every} (task "{TASK_NAME}"). '
        "Remove it with `ghost-api watch schedule --remove`."
    )


def _served(store_path: Path, sites: list[str] | None, quiet: Console) -> dict[str, Any]:
    from ghost_api.serve import load_sites

    with Store(store_path) as store:
        names = [_site(store, s) for s in sites] if sites else None
    try:
        served = load_sites(store_path, names)
    except OperationError as exc:
        quiet.print(str(exc))
        raise typer.Exit(code=1) from exc
    if not served:
        quiet.print("Nothing to serve: no stored site has endpoints Ghost API can call.")
        raise typer.Exit(code=1)
    return served


@app.command()
def diff(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Also call each endpoint once and compare the reply with what was captured "
            "(no browser).",
        ),
    ] = False,
    history: Annotated[bool, typer.Option("--history", help="List the client's versions.")] = False,
    since: Annotated[
        str | None,
        typer.Option(help="Changes recorded after this client version (e.g. after a refresh)."),
    ] = None,
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Print a pull-request description.")
    ] = False,
    fail_on: Annotated[
        str, typer.Option(help="Exit 1 on: breaking, any or never (for CI).")
    ] = "never",
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """What changed in a site's API since the last client version, and what it means."""
    from ghost_api import drift
    from ghost_api.generator import site_spec

    if fail_on not in ("breaking", "any", "never"):
        raise typer.BadParameter("--fail-on must be breaking, any or never")
    with Store(_need_db(db)) as store:
        domain = _site(store, domain)
        versions = store.api_versions(domain)
        if history:
            _print_history(domain, versions)
            return
        try:
            spec = site_spec(store, domain)
        except NothingToGenerate as exc:
            console.print(str(exc))
            raise typer.Exit(code=exitcodes.FAILED) from exc
        info = drift.version_for(store, domain, spec)
        changes = list(info.changes)
        if since is not None:
            info, changes = _since(store, domain, since, info, changes)
        if live:
            changes += _live_changes(store, domain)
    if live and not info.changes and changes:  # only the live check found something
        info = drift.VersionInfo(drift.bump(info.version, changes), changes, info.version,
                                 True, info.history)  # fmt: skip
    if _options["json"]:
        json_out(
            {"site": domain, "version": info.version, "previous": info.previous,
             "changes": drift.changes_json(changes),
             "breaking": sum(c.severity == "breaking" for c in changes)}
        )  # fmt: skip
    elif markdown:
        sys.stdout.write(drift.to_markdown(domain, info, changes) + "\n")
    elif not versions:
        console.print(
            f"No client version recorded for {domain} yet; the next `ghost-api generate` "
            f"or run makes {info.version}."
        )
    elif not changes:
        console.print(f"[green]No API changes[/] for {domain} since version {info.version}.")
    else:
        console.print(f"[bold]{domain}[/]: client {info.previous} -> {info.version}")
        for change in changes:
            style = {"breaking": "red", "additive": "green", "info": "dim"}[change.severity]
            console.print(f"  [{style}]{change.line()}[/]", highlight=False)
        console.print("[dim]Run `ghost-api generate` to write the new client version.[/]")
    breaking = any(c.severity == "breaking" for c in changes)
    if (fail_on == "breaking" and breaking) or (fail_on == "any" and changes):
        raise typer.Exit(code=exitcodes.FAILED)


def _since(
    store: Store, domain: str, since: str, info: Any, pending: list[Any]
) -> tuple[Any, list[Any]]:
    """Every change recorded after version `since`, plus any not yet recorded."""
    from ghost_api import drift

    versions = store.api_versions(domain)
    known = [v["version"] for v in versions]
    if since not in known:
        console.print(f"No version {since} for {domain}. Versions: {', '.join(known) or 'none'}")
        raise typer.Exit(code=exitcodes.USAGE)
    newer = versions[: known.index(since)]
    changes = [drift.Change(**c) for v in reversed(newer) for c in v["changes"]] + pending
    latest = info.version if pending else (newer[0]["version"] if newer else since)
    return drift.VersionInfo(latest, changes, since, bool(changes), versions), changes


def _print_history(domain: str, versions: list[dict[str, Any]]) -> None:
    if _options["json"]:
        json_out({"site": domain, "versions": [
            {k: v for k, v in entry.items() if k != "snapshot"} for entry in versions
        ]})  # fmt: skip
        return
    if not versions:
        console.print(f"No client versions recorded for {domain} yet.")
        return
    table = Table(title=f"Client versions for {domain}", title_justify="left")
    for col in ("Version", "Date", "Changes"):
        table.add_column(col, overflow="fold")
    for entry in versions:
        counts = [c["severity"] for c in entry["changes"]]
        summary = ", ".join(f"{counts.count(s)} {s}" for s in ("breaking", "additive", "info")
                            if counts.count(s)) or "first version"  # fmt: skip
        table.add_row(entry["version"], str(entry["created_at"])[:10], summary)
    console.print(table)


def _live_changes(store: Store, domain: str) -> list[Any]:
    """Call each operation once (read-only, rate-limited) and compare the reply's shape."""
    from ghost_api import drift
    from ghost_api.analysis.schema import build_schema

    try:
        rt = SiteRuntime(store, domain)
    except OperationError as exc:
        console.print(f"[yellow]Live check skipped:[/] {exc}")
        return []
    stored = {e.key: e.response_schema for e in store.current_endpoints(domain)}
    changes: list[Any] = []
    with rt:
        for op in rt.operations():
            inputs = {i.name: i.example for i in op.inputs if i.example}
            try:
                result = rt.call(op, inputs)
            except (OperationError, ReplayRefused) as exc:
                changes.append(drift.Change("info", op.name, f"not checked live: {exc}"))
                continue
            if not result.ok:
                status = result.status or "network error"
                changes.append(drift.Change("breaking", op.name, f"live call failed ({status})"))
                continue
            fresh = build_schema([result.body])
            changes += drift.live_changes(op.name, stored.get(op.endpoint.key), fresh)
            console.print(f"[dim]  checked {op.name} live[/]")
    return changes


@app.command()
def serve(
    sites: Annotated[
        list[str] | None, typer.Argument(help="Sites to serve (default: all stored sites).")
    ] = None,
    port: Annotated[int, typer.Option(help="Port on this computer.")] = 8900,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Serve sites as a clean local REST API (for Excel, scripts, any language)."""
    import uvicorn

    from ghost_api.serve.rest import create_rest_app
    from ghost_api.site import to_url_id

    served = _served(_need_db(db), sites, console)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    base = f"http://127.0.0.1:{port}"
    console.print(f"[bold {ACCENT}]◆ Ghost API REST[/]  {base}   docs: {base}/docs")
    for s in served.values():
        for op in s.operations:
            console.print(f"  GET {base}/{to_url_id(s.site)}/{op.name}", markup=False)
    console.print("[dim]Add &format=csv for spreadsheets. Local only. Ctrl+C to stop.[/]")
    uvicorn.run(create_rest_app(served, hosts), host="127.0.0.1", port=port, log_level="warning")


@app.command()
def mock(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    port: Annotated[int, typer.Option(help="Port on this computer.")] = 8901,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Serve a local copy of a site's API from captured replies: work offline, test
    front ends, never touch the real site."""
    import uvicorn

    from ghost_api.serve.mock import build_routes, create_mock_app

    with Store(_need_db(db)) as store:
        domain = _site(store, domain)
        routes = build_routes(store, domain)
    if not routes:
        console.print(f"No captured replies for {domain}. Run `ghost-api run` first.")
        raise typer.Exit(code=exitcodes.FAILED)
    base = f"http://127.0.0.1:{port}"
    if _options["json"]:
        json_out(
            {"site": domain, "base_url": base,
             "routes": [f"{r.endpoint.method} {r.endpoint.path_template}" for r in routes]}
        )  # fmt: skip
    console.print(f"[bold {ACCENT}]◆ Mock of {domain}[/]  {base}  (captured replies, no traffic)")
    for r in routes:
        console.print(
            f"  {r.endpoint.method:<6} {base}{r.endpoint.path_template}"
            f"  [dim]{len(r.samples)} repl{'y' if len(r.samples) == 1 else 'ies'}[/]"
        )
    console.print("[dim]Point your client's base URL here. Local only. Ctrl+C to stop.[/]")
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    uvicorn.run(create_mock_app(routes, domain, hosts), host="127.0.0.1", port=port,
                log_level="warning")  # fmt: skip


@app.command()
def mcp(
    sites: Annotated[
        list[str] | None, typer.Argument(help="Sites to offer (default: all stored sites).")
    ] = None,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Offer sites as tools to AI assistants (MCP over stdio), e.g. for Claude Code:
    claude mcp add ghost -- ghost-api mcp turftown.in/chennai"""
    import anyio

    stderr = Console(stderr=True)  # stdout carries the protocol
    try:
        from ghost_api.serve.mcp import run_stdio
    except ImportError as exc:
        stderr.print('The MCP server needs the mcp package: pip install "mcp>=1.2"')
        raise typer.Exit(code=1) from exc
    served = _served(_need_db(db), sites, stderr)
    count = sum(len(s.operations) for s in served.values())
    stderr.print(f"Ghost API MCP server: {count} tool(s) from {', '.join(served)}")
    anyio.run(run_stdio, served)


@app.command()
def forget(
    domain: Annotated[str | None, typer.Argument(help=SITE_HELP)] = None,
    llm_cache: Annotated[
        bool, typer.Option("--llm-cache", help="Also clear cached LLM answers (all sites).")
    ] = False,
    keep_files: Annotated[
        bool, typer.Option("--keep-files", help="Keep the output folder and screenshots.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
    out: Annotated[Path, typer.Option(help="Output directory.")] = Path("output"),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Delete what Ghost API stored about a site: runs, captured traffic, endpoints,
    watches, screenshots and its output folder."""
    import shutil

    from ghost_api.pipeline import screenshots_dir

    if domain is None and not llm_cache:
        console.print("Name a site to forget, e.g. ghost-api forget quotes.toscrape.com")
        raise typer.Exit(code=2)
    path = _need_db(db)
    with Store(path) as store:
        site = _site(store, domain) if domain else None
        what = f"everything stored for {site}" if site else ""
        if llm_cache:
            what += (" and " if what else "") + "the cached LLM answers"
        if not yes:
            from ghost_api.ui import keys

            with keys.input_paused():  # normal typing inside the interactive session too
                sure = typer.confirm(f"Delete {what}? This can't be undone.", default=False)
            if not sure:
                raise typer.Exit(code=1)
        deleted: dict[str, Any] = {"site": site, "rows": {}, "folders": [], "llm_cache": 0}
        if site:
            counts, run_ids = store.forget_site(site)
            rows = sum(counts.values())
            deleted["rows"] = counts
            console.print(f"Deleted {rows} stored row(s) for {site}.")
            site_out = (out / site).resolve()
            if not keep_files and rows and site_out.is_relative_to(out.resolve()):
                folders = [screenshots_dir(path, r) for r in run_ids] + [site_out]
                for folder in folders:
                    if folder.is_dir():
                        shutil.rmtree(folder, ignore_errors=True)
                        deleted["folders"].append(str(folder))
                        console.print(f"[dim]Removed {folder}[/]")
        if llm_cache:
            deleted["llm_cache"] = store.clear_llm_cache()
            console.print(f"Cleared {deleted['llm_cache']} cached LLM answer(s).")
        store.conn.execute("VACUUM")  # really remove the data from the file
    console.print("[dim]Copies you made elsewhere (clients, downloads) are not touched.[/]")
    if _options["json"]:
        json_out(deleted)


KEY_CHOICES = {
    "1": ("Gemini (Google, free tier)", "GEMINI_API_KEY", "https://aistudio.google.com/apikey"),
    "2": ("Groq (free tier)", "GROQ_API_KEY", "https://console.groq.com/keys"),
    "3": ("OpenRouter", "OPENROUTER_API_KEY", "https://openrouter.ai/keys"),
}


@app.command()
def init(
    lang: Annotated[str, typer.Option(help="Default client language: python or typescript.")] = (
        "python"
    ),
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Don't ask: install the browser, skip the key.")
    ] = False,
    skip_browser: Annotated[
        bool, typer.Option("--skip-browser", help="Don't download Chromium.")
    ] = False,
) -> None:
    """Set up Ghost API here: project file, browser, LLM key, .gitignore. Safe to re-run."""
    from ghost_api.browser.install import chromium_path, install_chromium
    from ghost_api.project import PROJECT_FILE, ensure_gitignore, write_project
    from ghost_api.ui import keys

    lang = _check_lang(lang)
    root = Path.cwd()
    done: dict[str, Any] = {"project": str(root / PROJECT_FILE)}

    def ask(question: str, default: bool = True) -> bool:
        if yes:
            return default
        with keys.input_paused():
            return typer.confirm(question, default=default)

    # 1. Project file
    if (root / PROJECT_FILE).exists():
        console.print(f"[dim]✓ {PROJECT_FILE} already here[/]")
        done["project_created"] = False
    else:
        write_project(root, lang)
        console.print(f"[green]✓[/] Created {PROJECT_FILE} (settings) and .ghost-api/ (data)")
        done["project_created"] = True
    if any((folder / ".git").exists() for folder in (root, *root.parents)):
        added = ensure_gitignore(root)
        done["gitignore_added"] = added
        if added:
            console.print(f"[green]✓[/] .gitignore now keeps out: {', '.join(added)}")

    # 2. Browser
    browser = chromium_path()
    question = "Chromium (the browser Ghost API drives) is missing. Download it now (~150 MB)?"
    if browser is None and not skip_browser and ask(question):
        console.print("[dim]Downloading Chromium...[/]")
        browser = chromium_path() if install_chromium() else None
    done["chromium"] = str(browser) if browser else None
    if browser:
        console.print("[green]✓[/] Chromium is installed")
    else:
        console.print(
            "[yellow]![/] No Chromium yet. Later: ghost-api init (or playwright install chromium)"
        )

    # 3. LLM key (optional: --no-agent runs work without one)
    has_key = any(p.api_key for p in default_providers())
    done["llm_key"] = has_key
    if has_key:
        console.print("[green]✓[/] An LLM key is set")
    elif not yes and ask("Add an LLM key now? (Without one, runs only load and scroll pages.)"):
        for number, (name, _env, url) in KEY_CHOICES.items():
            console.print(f"  {number}. {name}  [dim]{url}[/]")
        with keys.input_paused():
            choice = typer.prompt("Provider", default="1")
            name, env, _url = KEY_CHOICES.get(choice, KEY_CHOICES["1"])
            key = typer.prompt(f"Paste your {env} (hidden)", hide_input=True).strip()
        if key:
            env_file = root / ".env"
            lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
            lines = [line for line in lines if not line.startswith(f"{env}=")] + [f"{env}={key}"]
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.environ[env] = key
            done["llm_key"] = True
            console.print(f"[green]✓[/] Saved {env} in .env (keep this file private)")
    else:
        console.print("[dim]No LLM key: add one later with ghost-api init[/]")

    if _options["json"]:
        json_out(done)
        return
    console.print(
        "\n[bold]Ready.[/] Try:\n"
        '  ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes"\n'
        "  ghost-api            (interactive session)\n"
        "  ghost-api doctor     (check everything)"
    )


@app.command()
def doctor() -> None:
    """Check your setup: Python, the browser, the project, the database and each LLM key
    (one tiny call per provider)."""
    import platform

    from ghost_api.browser.install import chromium_path

    started = time.monotonic()
    project = find_project()
    browser = chromium_path()
    db = default_db_path()
    setup = [
        ("Python", platform.python_version(), sys.version_info >= (3, 11), "needs 3.11 or newer"),
        ("Chromium", str(browser or "not installed"), browser is not None,
         "run: ghost-api init (or: playwright install chromium)"),
        ("Project", str(project.file if project else "none (using this folder)"), True, ""),
        ("Database", f"{db} ({'exists' if db.exists() else 'created on the first run'})", True,
         ""),
    ]  # fmt: skip
    rows = check_providers(default_providers())
    working = sum(r["ok"] for r in rows)
    elapsed = time.monotonic() - started
    if _options["json"]:
        json_out(
            {"setup": [{"check": n, "value": v, "ok": ok, "fix": fix} for n, v, ok, fix in setup],
             "providers": rows, "working_providers": working, "seconds": round(elapsed, 1)}
        )  # fmt: skip
    else:
        checks = Table(title="Setup", title_justify="left")
        for col in ("Check", "Found", ""):
            checks.add_column(col, overflow="fold")
        for name, value, ok, fix in setup:
            checks.add_row(name, value, "[green]OK[/]" if ok else f"[red]missing:[/] {fix}")
        console.print(checks)
        table = Table(title="LLM providers", title_justify="left")
        for col in ("Provider", "Model", "Key", "Result", "Time"):
            table.add_column(col)
        for r in rows:
            if not r["key"]:
                table.add_row(r["provider"], r["model"], "[dim]missing[/]", "skipped", "-")
                continue
            result = "[green]OK[/]" if r["ok"] else f"[red]failed:[/] {r['detail'][:90]}"
            table.add_row(r["provider"], r["model"], "set", result, f"{r['seconds']:.1f}s")
        console.print(table)
        if working:
            console.print(
                f"{working} provider(s) working in {elapsed:.1f}s. "
                "Fallback order: GHOST_API_PROVIDERS in .env."
            )
        else:
            console.print(
                "[yellow]No LLM provider is working.[/] Runs still work with --no-agent "
                "(the page is loaded and scrolled, no AI).\n"
                "[bold]To add a key:[/] run ghost-api init, or put GEMINI_API_KEY=your-key "
                "(or GROQ_API_KEY / OPENROUTER_API_KEY) in a .env file in this folder.\n"
                "Free Gemini key: https://aistudio.google.com/apikey"
            )
    if not all(ok for _, _, ok, _ in setup) or not working:
        raise typer.Exit(code=exitcodes.FAILED)


@app.command()
def inspect(
    domain: Annotated[
        str | None, typer.Argument(help="Site to show (or its domain); omit for all.")
    ] = None,
    run_id: Annotated[
        int | None, typer.Option("--run", help="Replay one run's step log and test results.")
    ] = None,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Show what's stored for a site, or replay one run with --run."""
    path = _need_db(db)
    if run_id is not None:
        _replay_run(path, run_id)
        return
    with Store(path) as store:
        domain = _site(store, domain) if domain else None
        runs = store.list_runs(domain)
        endpoints = store.current_endpoints(domain) if domain else []
        total_endpoints = len(endpoints) if domain else _endpoint_total(store, runs)
        lessons = store.lessons_for(domain, limit=20) if domain else []
    if _options["json"]:
        json_out(
            {"site": domain, "sites": sorted({r.domain for r in runs}),
             "endpoint_count": total_endpoints, "runs": [asdict(r) for r in runs],
             "endpoints": [endpoint_summary(e, domain) for e in endpoints] if domain else [],
             "lessons": lessons}
        )  # fmt: skip
        return
    if not runs:
        console.print("No runs found.")
        return
    # Summary line above the table
    sites_set = {r.domain for r in runs}
    console.print(
        f"[bold]{len(sites_set)} site(s) explored, "
        f"{total_endpoints} endpoint(s) across {len(runs)} run(s).[/]"
    )
    table = Table(title="Runs")
    for col in ("ID", "Domain", "Goal", "Status", "Requests", "LLM calls", "Started"):
        table.add_column(col)
    for r in runs:
        table.add_row(
            str(r.id),
            r.domain,
            r.goal,
            r.status,
            str(r.request_count),
            str(r.llm_calls),
            r.started_at,
        )
    console.print(table)
    if domain:
        if endpoints:
            summaries = [endpoint_summary(e, domain) for e in endpoints]
            console.print(endpoint_table(summaries, f"Endpoints ({len(summaries)})"))
        else:
            console.print("No endpoints stored for this domain.")
        if lessons:
            console.print("[bold]Lessons for next time[/] (newest first):")
            for lesson in lessons:
                console.print(f"  - {lesson}")


def _endpoint_total(store: Store, runs: list[RunRow]) -> int:
    total = 0
    for site in {r.domain for r in runs}:
        total += len(store.current_endpoints(site))
    return total


RESULT_STYLE = {"confirmed": "green", "rejected": "red", "inconclusive": "yellow"}


def _replay_run(path: Path, run_id: int) -> None:
    """Print a past run step by step, for debugging (NFR-33)."""
    with Store(path) as store:
        run_row = store.get_run(run_id)
        if run_row is None:
            console.print(f"Run #{run_id} not found.")
            raise typer.Exit(code=1)
        steps = store.steps_for_run(run_id)
        hypotheses = store.hypotheses_for_run(run_id)
    console.print(f"[bold]Run #{run_row.id}[/] on {run_row.domain}: {run_row.goal}")
    console.print(
        f"Status {run_row.status}, {run_row.request_count} requests, {run_row.llm_calls} LLM "
        f"calls, started {run_row.started_at}"
    )
    for s in steps:
        step_args = s["args"] if isinstance(s["args"], dict) else {}
        args = ", ".join(f"{k}={v!r}" for k, v in step_args.items())
        console.print(f"[bold]Step {s['step_no']}[/] {s['action']}({args})  [dim]{s['thought']}[/]")
        console.print(f"   [dim]{s['result']}[/]")
    if not steps:
        console.print("[dim]No agent steps in this run.[/]")
    for h in hypotheses:
        style = RESULT_STYLE[str(h["result"])]
        label = f"[{style}]{h['result']:<12}[/] {h['endpoint_key']}: {h['text']}"
        console.print(f"  {label}  [dim]{h['evidence']}[/]")
