"""Command-line entry point: `ghost-api [run | refresh | generate | verify | report | inspect |
fetch | watch | serve | mcp | doctor | ui]`. With no command, asks for a URL and goal interactively.

The workflow itself lives in `ghost_api/pipeline.py` and emits events; this module parses
options and renders those events with `ghost_api/ui/terminal.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from ghost_api import __version__, watch
from ghost_api.config import SUPPORTED_LANGUAGES, Settings, default_db_path, load_env
from ghost_api.events import EventBus, Notice
from ghost_api.export import tabular, write_exports
from ghost_api.export.site_data import endpoint_summary
from ghost_api.export.tabular import FORMATS
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
from ghost_api.probe.replay import ReplayRefused
from ghost_api.runtime import OperationError, SiteRuntime
from ghost_api.site import SiteAmbiguous, resolve_site, site_host
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
    gateway = None if no_llm else LLMGateway(default_providers(), max_calls=3)
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
    print_client(console, client_event(result))


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
    path = _need_db(db)
    renderer = TerminalRenderer(console, plain=True)
    with Store(path) as store:
        domain = _site(store, domain)
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
    console.print(f"Report:  {exports.report}")
    if exports.html:
        console.print(f"Report (shareable page): {exports.html}")
    console.print(f"OpenAPI: {exports.openapi}")


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
        if operation is None:
            console.print(_operations_table(rt))
            ops = rt.operations()
            if ops:
                console.print(f"[dim]Example: ghost-api fetch {domain} {ops[0].name}[/]")
            else:
                console.print("Nothing here can be fetched without the browser.")
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
    console.print(
        f"[bold green]Saved {len(records)} rows[/] to {target}"
        + (f"  [dim]({hidden} personal values hidden)[/]" if hidden else "")
    )


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
    console.print("Checking due watches every minute. Press Ctrl+C to stop.")
    with Store(_need_db(db)) as store:
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
