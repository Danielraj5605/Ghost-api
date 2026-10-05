"""Command-line entry point: `ghost-api run | refresh | inspect`."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.table import Table

from ghost_api import __version__
from ghost_api.browser.driver import BrowserSession
from ghost_api.capture.models import CapturedRequest
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.config import Settings, default_db_path, load_env
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter, check_robots

app = typer.Typer(
    help="Ghost API: turn any website into a typed, tested API client.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# Until the agent loop exists (Stage 4), `run` explores by scrolling a few times.
AUTO_SCROLLS = 5


def _version(value: bool) -> None:
    if value:
        console.print(f"ghost-api {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    load_env()


def _normalize_url(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise typer.BadParameter(f"Not a valid http(s) URL: {url}")
    return url


def _short(url: str, width: int = 80) -> str:
    parts = urlsplit(url)
    text = parts.path + (f"?{parts.query}" if parts.query else "")
    return text if len(text) <= width else text[: width - 3] + "..."


@app.command()
def run(
    url: Annotated[str, typer.Argument(help="Website to explore, e.g. https://example.com")],
    goal: Annotated[str, typer.Option("--goal", "-g", help="What the client should do.")],
    lang: Annotated[str, typer.Option(help="Output language.")] = "python",
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
) -> None:
    """Explore a website and capture its hidden API traffic."""
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

    host = urlsplit(settings.url).hostname or ""
    limiter = RateLimiter(settings.rate_limit)

    # Stage 0 of the workflow: pre-checks.
    if settings.ignore_robots:
        console.print("[yellow]![/] robots.txt check skipped (--ignore-robots)")
    else:
        verdict = check_robots(settings.url, limiter)
        if not verdict.allowed:
            console.print(f"[red]Stopped:[/] {verdict.reason} for {settings.url}")
            console.print("Use --ignore-robots only if you have permission to access this site.")
            raise typer.Exit(code=2)
        console.print(f"[green]OK[/] {verdict.reason}")
        if verdict.crawl_delay:
            limiter.slow_down_to(verdict.crawl_delay)
            console.print(f"   honoring Crawl-delay of {verdict.crawl_delay}s")

    console.print(f"[bold]Goal:[/] {settings.goal}")
    console.print("[bold]Exploring site...[/]")

    def show(req: CapturedRequest) -> None:
        console.print(f"   [cyan]\\[captured][/] {req.method:<6} {req.status}  {_short(req.url)}")

    started = time.monotonic()
    status = "failed"
    with Store(settings.db_path) as store:
        run_id = store.create_run(host, settings.url, settings.goal)

        def on_capture(req: CapturedRequest) -> None:
            store.add_request(run_id, req)
            show(req)

        recorder = TrafficRecorder(host, on_capture=on_capture)
        try:
            with BrowserSession(settings.url, recorder, headed=settings.headed) as browser:
                browser.goto(settings.url)
                for _ in range(AUTO_SCROLLS):
                    browser.scroll("bottom")
            status = "captured"
        except KeyboardInterrupt:
            status = "interrupted"
            console.print("[yellow]Interrupted. Partial results were saved.[/]")
        except Exception as exc:
            console.print(f"[red]Browser error:[/] {exc}")
        finally:
            store.finish_run(run_id, status)

        _print_summary(recorder, time.monotonic() - started, run_id, settings.db_path)
    if status == "failed":
        raise typer.Exit(code=1)


def _print_summary(recorder: TrafficRecorder, seconds: float, run_id: int, db: Path) -> None:
    captured = recorder.captured
    console.print()
    if captured:
        table = Table(title=f"Captured API requests ({len(captured)})")
        table.add_column("Method")
        table.add_column("Status", justify="right")
        table.add_column("URL")
        for req in captured:
            table.add_row(req.method, str(req.status), _short(req.url, 90))
        console.print(table)
    else:
        console.print(
            "[yellow]No JSON API requests captured.[/] The site may render on the server."
        )
    if recorder.dropped:
        noise = ", ".join(f"{k}: {v}" for k, v in recorder.dropped.most_common())
        console.print(f"[dim]Filtered out: {noise}[/]")
    console.print(f"[dim]Run #{run_id} saved to {db} in {seconds:.1f}s[/]")


@app.command()
def refresh(domain: Annotated[str, typer.Argument(help="Domain explored before.")]) -> None:
    """Re-verify a known site using stored memory (available from Stage 8)."""
    console.print(f"[yellow]`refresh` is not implemented yet.[/] (planned for Stage 8: {domain})")
    raise typer.Exit(code=1)


@app.command()
def inspect(
    domain: Annotated[str | None, typer.Argument(help="Domain to show; omit for all.")] = None,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Show what's stored for a site."""
    path = db or default_db_path()
    if not path.exists():
        console.print(f"No database at {path}. Run `ghost-api run` first.")
        raise typer.Exit(code=1)
    with Store(path) as store:
        runs = store.list_runs(domain)
    if not runs:
        console.print("No runs found.")
        return
    table = Table(title="Runs")
    for col in ("ID", "Domain", "Goal", "Status", "Requests", "Started"):
        table.add_column(col)
    for r in runs:
        table.add_row(str(r.id), r.domain, r.goal, r.status, str(r.request_count), r.started_at)
    console.print(table)
