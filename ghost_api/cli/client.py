"""Commands about the generated client: generate, verify, report, diff, mock."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table
from rich.text import Text

from ghost_api import exitcodes
from ghost_api.cli._common import (
    SITE_HELP,
    _check_lang,
    _need_db,
    _options,
    _site,
    app,
    console,
    json_out,
)
from ghost_api.config import (
    default_db_path,
    state_dir,
)
from ghost_api.export import write_exports
from ghost_api.export.site_data import endpoint_summary
from ghost_api.generator import NothingToGenerate, generate_client
from ghost_api.generator.python_gen import GenerationError
from ghost_api.llm.gateway import LLMGateway
from ghost_api.llm.providers import default_providers
from ghost_api.memory.store import Store
from ghost_api.pipeline import (
    client_event,
    round_event,
)
from ghost_api.ui.terminal import (
    ACCENT,
    TerminalRenderer,
    endpoint_table,
    file_link,
    print_client,
)
from ghost_api.verify.verifier import Verifier


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
        if not any(c.source == "live" for c in changes):
            console.print(
                "[dim]These are differences in the generated client: from the site's data, or "
                "from a newer Ghost API typing things differently. --live checks the site "
                "itself.[/]"
            )
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
    from ghost_api import drift

    return drift.live_check(
        store, domain, on_progress=lambda text: console.print(f"[dim]  {text}[/]")
    )


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
def adopt(
    path: Annotated[Path, typer.Argument(help="The codebase to scan.")] = Path("."),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Find hand-written calls to websites' hidden APIs in a codebase, and the typed client
    method that replaces each one (for sites Ghost API has explored). Nothing is edited."""
    from ghost_api.adopt import adopt as find_calls
    from ghost_api.adopt import to_json

    database = db or default_db_path()
    store = Store(database) if database.exists() else None
    try:
        reports = find_calls(path.resolve(), store)
    finally:
        if store is not None:
            store.close()
    if _options["json"]:
        json_out({"path": str(path.resolve()), "hosts": to_json(reports)})
        return
    if not reports:
        console.print("No HTTP calls with a literal URL found.")
        return
    total = sum(len(r.calls) for r in reports)
    console.print(f"[bold]{total} call(s) to {len(reports)} host(s)[/]")
    for report in reports:
        known = f"explored as {report.site}" if report.site else "not explored yet"
        console.print(f"\n[bold {ACCENT}]{report.host}[/]  [dim]{known}[/]")
        table = Table(box=None, pad_edge=False, show_header=False)
        for col in ("Where", "Call", "Replace with"):
            table.add_column(col, overflow="fold")
        for call in report.calls:
            table.add_row(
                f"[dim]{call.file}:{call.line}[/]",
                f"{call.method} {call.url}",
                f"[green]{call.replacement}[/]" if call.replacement else "[dim]-[/]",
            )
        console.print(table)
        console.print(f"  [dim]Next: {report.next_step}[/]")


@app.command("verify-spec")
def verify_spec(
    spec_file: Annotated[Path, typer.Argument(help="A published OpenAPI file (YAML or JSON).")],
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Compare a published OpenAPI spec with what the site really does: undocumented
    endpoints, documented ones never seen, and reply fields that differ."""
    from ghost_api.specdiff import SpecError, compare, load_spec, to_json

    try:
        spec = load_spec(spec_file)
    except SpecError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=exitcodes.USAGE) from exc
    with Store(_need_db(db)) as store:
        domain = _site(store, domain)
        checks = compare(spec, store.current_endpoints(domain))
    if _options["json"]:
        json_out({"site": domain, "spec": str(spec_file), "endpoints": to_json(checks)})
        return
    counts = {s: sum(c.status == s for c in checks) for s in
              ("documented", "undocumented", "not seen")}  # fmt: skip
    console.print(
        f"[bold]{domain}[/] vs {spec_file.name}: {counts['documented']} documented and seen, "
        f"[yellow]{counts['undocumented']} undocumented[/], "
        f"[dim]{counts['not seen']} documented but not seen[/]"
    )
    for check in checks:
        style = {"documented": "green", "undocumented": "yellow", "not seen": "dim"}[check.status]
        console.print(f"  [{style}]{check.status:<13}[/] {check.endpoint}", highlight=False)
        if check.extra_fields:
            console.print(f"      [yellow]replies also have:[/] {', '.join(check.extra_fields)}")
        if check.missing_fields:
            console.print(
                f"      [dim]spec lists, never seen:[/] {', '.join(check.missing_fields)}"
            )


@app.command()
def explain(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """A plain-language summary of a site's API for someone new to it (no LLM)."""
    from ghost_api.generator import site_spec

    with Store(_need_db(db)) as store:
        domain = _site(store, domain)
        endpoints = store.current_endpoints(domain)
        versions = store.api_versions(domain)
        runs = store.list_runs(domain)
        try:
            spec = site_spec(store, domain)
            names = {m.endpoint_key: m.name for m in spec.methods}
            class_name = spec.class_name
        except NothingToGenerate:
            names, class_name = {}, None
    if not endpoints:
        console.print(f"Nothing is known about {domain} yet. Run `ghost-api run` first.")
        raise typer.Exit(code=exitcodes.FAILED)
    summaries = [endpoint_summary(e, domain) for e in endpoints]
    login = {s["auth_type"] for s in summaries if s.get("auth_type") not in (None, "none")}
    if _options["json"]:
        json_out({"site": domain, "endpoints": summaries, "client": class_name,
                  "version": versions[0]["version"] if versions else None})  # fmt: skip
        return
    last = runs[0].started_at[:10] if runs else "?"
    version = f", client {versions[0]['version']}" if versions else ""
    console.print(f"[bold]{domain}[/]  [dim]explored {last}{version}[/]\n")
    console.print(
        f"It has {len(endpoints)} API endpoint{'s' if len(endpoints) != 1 else ''}"
        + (
            f"; some need a login or key ({', '.join(sorted(login))})."
            if login
            else "; none needs a login."
        )  # fmt: skip
    )
    for ep, s in zip(endpoints, summaries, strict=True):
        fields = ", ".join(s.get("fields") or []) or "data"
        paging = (s.get("pagination") or {}).get("style")
        how = f", page by page ({paging})" if paging else ""
        state = {"verified": "works", "failed": "failing", "unverified": "not tested"}.get(
            str(s.get("status")), str(s.get("status") or "seen")
        )
        method = names.get(ep.key)
        call = f"  -> {class_name}.{method}()" if method and class_name else ""
        console.print(
            f"  [bold]{s['method']} {s['path']}[/]: returns {fields}{how} [dim]({state})[/]{call}",
            highlight=False,
        )
    console.print(
        f"\n[dim]Data without code: ghost-api fetch {domain}   ·   "
        f"client: ghost-api generate {domain}   ·   changes: ghost-api diff {domain} --live[/]"
    )
