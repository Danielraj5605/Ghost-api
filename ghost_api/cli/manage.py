"""Commands that manage Ghost API itself: init, doctor, inspect, forget."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

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
)
from ghost_api.export.site_data import endpoint_summary
from ghost_api.llm.doctor import check_providers
from ghost_api.llm.providers import default_providers
from ghost_api.memory.store import RunRow, Store
from ghost_api.project import find_project
from ghost_api.ui import keys
from ghost_api.ui.terminal import (
    endpoint_table,
)


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
            from ghost_api.browser import auth_state

            if auth_state.delete(path, site):
                console.print(f"[dim]Deleted the saved login for {site}[/]")
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
