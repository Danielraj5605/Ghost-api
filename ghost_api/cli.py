"""Command-line entry point: `ghost-api [run | refresh | generate | verify | report | inspect |
doctor | ui]`. With no command, asks for a URL and goal interactively.

The workflow itself lives in `ghost_api/pipeline.py` and emits events; this module parses
options and renders those events with `ghost_api/ui/terminal.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from ghost_api import __version__
from ghost_api.config import SUPPORTED_LANGUAGES, Settings, default_db_path, load_env
from ghost_api.events import EventBus, Notice
from ghost_api.export import write_exports
from ghost_api.export.site_data import endpoint_summary
from ghost_api.generator import NothingToGenerate, generate_client
from ghost_api.generator.python_gen import GenerationError
from ghost_api.llm.doctor import check_providers
from ghost_api.llm.gateway import LLMGateway
from ghost_api.llm.providers import default_providers
from ghost_api.memory.knowledge import load_memory, memory_prompt
from ghost_api.memory.store import Store
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
from ghost_api.ui.terminal import ACCENT, TerminalRenderer, endpoint_table, print_client
from ghost_api.verify.verifier import Verifier

app = typer.Typer(
    help="Ghost API: turn any website into a typed, tested API client.",
    add_completion=False,
)
console = Console()
_options = {"plain": False}

EXAMPLES = [
    ("https://quotes.toscrape.com/scroll", "list quotes with their authors and tags"),
    ("https://hn.algolia.com/", "search Hacker News stories and comments"),
]


def _version(value: bool) -> None:
    if value:
        console.print(f"ghost-api {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version."),
    ] = None,
    plain: Annotated[
        bool, typer.Option("--plain", help="Simple line-by-line output (for logs and CI).")
    ] = False,
) -> None:
    load_env()
    for stream in (sys.stdout, sys.stderr):
        safe_output(stream)
    _options["plain"] = plain
    if ctx.invoked_subcommand is None:
        if not sys.stdin.isatty():
            console.print(ctx.get_help())
            raise typer.Exit()
        _interactive(ctx)


def safe_output(stream: object) -> None:
    """Never crash on a character the output can't show (e.g. '◆' when output is
    redirected to a file on Windows, which uses cp1252): print a '?' instead."""
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    reconfigure = getattr(stream, "reconfigure", None)
    if encoding != "utf8" and reconfigure is not None:
        reconfigure(errors="replace")


def _interactive(ctx: typer.Context) -> None:
    """`ghost-api` on its own: ask what to explore, then run."""
    console.print(
        Text.assemble(
            ("◆ Ghost API ", f"bold {ACCENT}"), ("turn any website into a typed API client", "dim")
        )
    )
    console.print(Text("  Examples:", style="dim"))
    for url, goal in EXAMPLES:
        console.print(Text(f"    {url}  ·  {goal}", style="dim"))
    console.print()
    url = Prompt.ask(f"[{ACCENT}]Website[/]", default=EXAMPLES[0][0])
    default_goal = next((g for u, g in EXAMPLES if u == url), "list the main data on this site")
    goal = Prompt.ask(f"[{ACCENT}]Goal[/]", default=default_goal)
    lang = Prompt.ask(
        f"[{ACCENT}]Client language[/]", choices=list(SUPPORTED_LANGUAGES), default="python"
    )
    ctx.invoke(run, url=url, goal=goal, lang=lang)


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
    gateway = LLMGateway(default_providers(), max_calls=max_calls)
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
    if lang not in SUPPORTED_LANGUAGES:
        raise typer.BadParameter(
            f"Unsupported language '{lang}'. Supported: {', '.join(SUPPORTED_LANGUAGES)}"
        )
    return lang


def _need_db(db: Path | None) -> Path:
    path = db or default_db_path()
    if not path.exists():
        console.print(f"No database at {path}. Run `ghost-api run` first.")
        raise typer.Exit(code=1)
    return path


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

    robots, allowed = robots_policy(settings.url, settings.ignore_robots, limiter, bus)
    if not allowed:
        raise typer.Exit(code=2)
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
            known = load_memory(store, host)
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
    if status in ("failed", "llm-unavailable"):
        raise typer.Exit(code=1)


@app.command()
def refresh(
    domain: Annotated[str, typer.Argument(help="Domain explored before, e.g. quotes.toscrape.com")],
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
    path = _need_db(db)
    lang = _check_lang(lang)
    with Store(path) as store:
        mem = load_memory(store, domain)
        if mem is None:
            console.print(f"Nothing remembered for {domain}. Run `ghost-api run` first.")
            raise typer.Exit(code=1)
        bus = EventBus()
        renderer = _renderer()
        bus.subscribe(renderer.handle)
        bus.emit(
            Notice(
                f"Refreshing {domain} from memory: {len(mem.endpoints)} endpoint(s), "
                f"{len(mem.lessons)} lesson(s)"
            )
        )
        limiter = RateLimiter(rate_limit)
        robots, allowed = robots_policy(mem.start_url, ignore_robots, limiter, bus)
        if not allowed:
            raise typer.Exit(code=2)
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
                domain,
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
            )
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


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="Port for the local dashboard.")] = 8800,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Don't open the browser automatically.")
    ] = False,
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Open the web dashboard: start runs, watch the agent live, browse every result."""
    from ghost_api.web.server import serve

    serve(db or default_db_path(), out, port=port, open_browser=not no_open, console=console)


@app.command()
def generate(
    domain: Annotated[str, typer.Argument(help="Domain explored before, e.g. quotes.toscrape.com")],
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Use names derived from URLs (no LLM call).")
    ] = False,
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """(Re)generate the client for a site from its stored endpoint map."""
    path = _need_db(db)
    gateway = None if no_llm else LLMGateway(default_providers(), max_calls=3)
    if gateway is not None and not gateway.providers:
        gateway = None
    try:
        with Store(path) as store:
            if gateway is not None:
                gateway.store = store
            result = generate_client(store, domain, out, llm=gateway, lang=_check_lang(lang))
    except (NothingToGenerate, GenerationError) as exc:
        console.print(f"[red]No client generated:[/] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        if gateway is not None:
            gateway.close()
    print_client(console, client_event(result))


@app.command()
def verify(
    domain: Annotated[str, typer.Argument(help="Domain explored before, e.g. quotes.toscrape.com")],
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Regenerate a site's client, run its tests against the live API, and repair.

    Uses stored data only. Endpoints that need a key are tested when the key is set in
    the environment (the generated client says which variable).
    """
    path = _need_db(db)
    renderer = TerminalRenderer(console, plain=True)
    with Store(path) as store:
        run_id = store.latest_endpoint_run(domain)
        run_row = store.latest_run(domain)
        if run_id is None or run_row is None:
            console.print(f"No endpoints stored for {domain}. Run `ghost-api run` first.")
            raise typer.Exit(code=1)
        endpoints = store.load_endpoints(domain, run_id)
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
    if any(state == "failed" for state, _ in report.statuses.values()):
        raise typer.Exit(code=1)


@app.command()
def report(
    domain: Annotated[str, typer.Argument(help="Domain explored before, e.g. quotes.toscrape.com")],
    run_id: Annotated[
        int | None, typer.Option("--run", help="Run to report on (default: the latest).")
    ] = None,
    out: Annotated[Path, typer.Option(help="Folder for generated files.")] = Path("output"),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Write report.md, report.html and openapi.yaml for a site from what is stored."""
    path = _need_db(db)
    with Store(path) as store:
        run_row = store.get_run(run_id) if run_id else store.latest_run(domain)
        if run_row is None or run_row.domain != domain:
            console.print(f"No run found for {domain}.")
            raise typer.Exit(code=1)
        exports = write_exports(store, domain, run_row.id, out)
    console.print(f"Report:  {exports.report}")
    if exports.html:
        console.print(f"Report (shareable page): {exports.html}")
    console.print(f"OpenAPI: {exports.openapi}")


@app.command()
def doctor() -> None:
    """Check that each LLM provider in .env answers (uses one tiny call per provider)."""
    table = Table(title="LLM providers")
    for col in ("Provider", "Model", "Key", "Result", "Time"):
        table.add_column(col)
    rows = check_providers(default_providers())
    for r in rows:
        if not r["key"]:
            table.add_row(r["provider"], r["model"], "[dim]missing[/]", "skipped", "-")
            continue
        result = "[green]OK[/]" if r["ok"] else f"[red]failed:[/] {r['detail'][:90]}"
        table.add_row(r["provider"], r["model"], "set", result, f"{r['seconds']:.1f}s")
    console.print(table)
    working = sum(r["ok"] for r in rows)
    if not working:
        console.print("[red]No provider is working.[/] Check the keys in .env.")
        raise typer.Exit(code=1)
    console.print(f"{working} provider(s) working. Fallback order: GHOST_API_PROVIDERS in .env.")


@app.command()
def inspect(
    domain: Annotated[str | None, typer.Argument(help="Domain to show; omit for all.")] = None,
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
        runs = store.list_runs(domain)
        latest = store.latest_endpoint_run(domain) if domain else None
        endpoints = store.load_endpoints(domain, latest) if domain and latest else []
        lessons = store.lessons_for(domain, limit=20) if domain else []
    if not runs:
        console.print("No runs found.")
        return
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
