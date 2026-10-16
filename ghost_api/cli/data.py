"""Commands that use a site's data: fetch, watch, serve, mcp, ui."""

from __future__ import annotations

import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ghost_api import watch
from ghost_api.cli._common import (
    SITE_HELP,
    _need_db,
    _options,
    _site,
    app,
    console,
    json_out,
)
from ghost_api.config import (
    default_db_path,
)
from ghost_api.export import tabular
from ghost_api.export.tabular import FORMATS
from ghost_api.memory.store import Store
from ghost_api.probe.replay import ReplayRefused
from ghost_api.runtime import OperationError, SiteRuntime
from ghost_api.ui.terminal import (
    ACCENT,
    file_link,
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
    explore: Annotated[
        bool,
        typer.Option(
            "--explore",
            help="Ghost API as a toolset: the assistant explores new sites (no LLM key "
            "needed) and uses explored ones (fetch, diff, generate).",
        ),
    ] = False,
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
) -> None:
    """Offer Ghost API to AI assistants (MCP over stdio), e.g. for Claude Code:
    claude mcp add ghost-api -- ghost-api mcp --explore      (explore any site)
    claude mcp add ghost -- ghost-api mcp turftown.in/chennai  (one tool per operation)"""
    import anyio

    stderr = Console(stderr=True)  # stdout carries the protocol
    try:
        from ghost_api.serve.mcp import run_stdio
    except ImportError as exc:
        stderr.print('The MCP server needs the mcp package: pip install "mcp>=1.2"')
        raise typer.Exit(code=1) from exc
    if explore:
        from ghost_api.serve import explorer_mcp

        stderr.print("Ghost API MCP server: explore sites and use explored ones")
        anyio.run(explorer_mcp.run_stdio, db, out)
        return
    served = _served(_need_db(db), sites, stderr)
    count = sum(len(s.operations) for s in served.values())
    stderr.print(f"Ghost API MCP server: {count} tool(s) from {', '.join(served)}")
    anyio.run(run_stdio, served)


app.add_typer(watch_app, name="watch")
