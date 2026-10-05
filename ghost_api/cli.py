"""Command-line entry point: `ghost-api run | doctor | inspect | refresh`."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from ghost_api import __version__
from ghost_api.agent.orchestrator import Explorer, StepRecord
from ghost_api.analysis.analyzer import analyze, endpoint_keys
from ghost_api.analysis.models import Endpoint
from ghost_api.analysis.schema import top_level_fields
from ghost_api.browser.driver import BrowserSession
from ghost_api.capture.models import CapturedRequest
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.config import Settings, default_db_path, load_env
from ghost_api.llm.gateway import LLMError, LLMGateway
from ghost_api.llm.providers import default_providers
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter, RobotsPolicy, fetch_robots
from ghost_api.probe.prober import Prober, ProbeReport
from ghost_api.probe.replay import Replayer

app = typer.Typer(
    help="Ghost API: turn any website into a typed, tested API client.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# With --no-agent, `run` just scrolls to the bottom this many times.
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
    no_agent: Annotated[
        bool, typer.Option("--no-agent", help="Don't use the LLM; just load and scroll the page.")
    ] = False,
    no_probe: Annotated[
        bool, typer.Option("--no-probe", help="Don't test endpoints by calling them directly.")
    ] = False,
    max_probes: Annotated[
        int, typer.Option(help="Maximum direct API calls for testing endpoints.")
    ] = 60,
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

    host = urlsplit(settings.url).hostname or ""
    limiter = RateLimiter(settings.rate_limit)

    # Workflow stage 0: pre-checks.
    if settings.ignore_robots:
        robots = RobotsPolicy.allow_all(host)
        console.print("[yellow]![/] robots.txt check skipped (--ignore-robots)")
    else:
        robots = fetch_robots(settings.url, limiter)
        verdict = robots.verdict(settings.url)
        if not verdict.allowed:
            console.print(f"[red]Stopped:[/] {verdict.reason} for {settings.url}")
            console.print("Use --ignore-robots only if you have permission to access this site.")
            raise typer.Exit(code=2)
        console.print(f"[green]OK[/] {verdict.reason}")
        if verdict.crawl_delay:
            limiter.slow_down_to(verdict.crawl_delay)
            console.print(f"   honoring Crawl-delay of {verdict.crawl_delay}s")

    gateway: LLMGateway | None = None
    if not no_agent:
        gateway = LLMGateway(default_providers(), max_calls=settings.max_llm_calls)
        if not gateway.providers:
            console.print(
                "[red]No LLM API key found.[/] Add one to .env (see .env.example), "
                "or use --no-agent to only scroll the page."
            )
            raise typer.Exit(code=2)

    console.print(f"[bold]Goal:[/] {settings.goal}")
    console.print("[bold]Exploring site...[/]")

    started = time.monotonic()
    status = "failed"
    reason = ""
    with Store(settings.db_path) as store:
        run_id = store.create_run(host, settings.url, settings.goal)
        if gateway is not None:
            gateway.store = store  # cache LLM answers in the same database

        def on_capture(req: CapturedRequest) -> None:
            store.add_request(run_id, req)
            tag = "third-party " if req.third_party else ""
            console.print(
                f"      [dim cyan]{tag}{req.method} {req.status} {_short(req.url, 70)}[/]"
            )

        def on_step(rec: StepRecord) -> None:
            store.add_step(
                run_id, rec.step, rec.thought, rec.action, rec.args, rec.result,
                len(rec.new_endpoints),
            )  # fmt: skip
            args = ", ".join(f"{k}={v!r}" for k, v in rec.args.items())
            console.print(f"[bold]Step {rec.step}[/] {rec.action}({args})  [dim]{rec.thought}[/]")
            for label in rec.new_endpoints:
                console.print(f"   [green]+ new endpoint[/] {label}")
            if rec.result.startswith(("rejected", "failed")):
                console.print(f"   [yellow]{rec.result}[/]")

        recorder = TrafficRecorder(host, on_capture=on_capture)
        replayer = Replayer(host, limiter, robots, max_requests=max_probes)
        ask_user = _ask_user if settings.headed and sys.stdin.isatty() else None
        try:
            with BrowserSession(settings.url, recorder, headed=settings.headed) as browser:
                if gateway is None:
                    browser.goto(settings.url)
                    for _ in range(AUTO_SCROLLS):
                        browser.scroll("bottom")
                    status, reason = "captured", "--no-agent: scrolled the page"
                else:
                    explorer = Explorer(
                        browser=browser,
                        llm=gateway,
                        captured=lambda: recorder.captured,
                        goal=settings.goal,
                        start_url=settings.url,
                        target_host=host,
                        max_steps=settings.max_steps,
                        max_minutes=settings.max_minutes,
                        robots=robots,
                        on_step=on_step,
                        replayer=replayer,
                        raw_requests=lambda: recorder.raw,
                        add_sample=recorder.add,
                        ask_user=ask_user,
                    )
                    result = explorer.run()
                    status, reason = result.status, result.reason
        except KeyboardInterrupt:
            status, reason = "interrupted", "stopped by user"
        except Exception as exc:
            console.print(f"[red]Browser error:[/] {exc}")
            reason = str(exc)
        finally:
            store.finish_run(run_id, status)
            if gateway is not None:
                store.record_usage(run_id, gateway.usage.calls, gateway.usage.tokens)
                gateway.close()

        # Workflow stage 4 (hypothesize & test): call each endpoint directly.
        reports: dict[str, ProbeReport] = {}
        if not no_probe and status not in ("interrupted", "bot-protection", "failed"):
            reports = _probe_all(recorder, replayer)
        replayer.close()

        # The final endpoint map, including samples gathered while probing.
        endpoints = analyze(recorder.captured)
        store.save_endpoints(host, run_id, endpoints)
        for ep in endpoints:
            report = reports.get(ep.key)
            if report is not None:
                _apply(ep, report)
                store.save_probe_report(host, run_id, ep, report)
        console.print()
        console.print(f"[bold]Finished:[/] {status} ({reason})")
        _print_summary(recorder, endpoints, host, time.monotonic() - started, run_id)
        if gateway is not None:
            u = gateway.usage
            providers = ", ".join(f"{k}: {v}" for k, v in u.by_provider.items()) or "-"
            console.print(
                f"[dim]LLM: {u.calls} calls ({providers}), {u.tokens:,} tokens, "
                f"{u.cache_hits} cached, {u.retries} retries, {u.repairs} repairs, "
                f"{u.fallbacks} fallbacks[/]"
            )
            for problem, count in u.problems.most_common(5):
                console.print(f"[dim]   {count}x {problem}[/]")
        console.print(f"[dim]Saved to {settings.db_path}[/]")
    if status in ("failed", "llm-unavailable"):
        raise typer.Exit(code=1)


def _ask_user(question: str) -> str:
    console.print(f"\n[bold yellow]The agent asks:[/] {question}")
    return console.input("[bold]Your answer[/] (press Enter when done): ")


RESULT_STYLE = {"confirmed": "green", "rejected": "red", "inconclusive": "yellow"}


def _probe_all(recorder: TrafficRecorder, replayer: Replayer) -> dict[str, ProbeReport]:
    endpoints = analyze(recorder.captured)
    if not endpoints:
        return {}
    console.print()
    console.print(f"[bold]Testing {len(endpoints)} endpoint(s) with direct calls...[/]")
    keys = endpoint_keys(recorder.captured)
    raws = list(recorder.raw)
    prober = Prober(replayer, on_sample=recorder.add)
    reports: dict[str, ProbeReport] = {}
    for ep in endpoints:
        samples = [r for k, r in zip(keys, raws, strict=False) if k == ep.key and r]
        report = prober.probe(ep, samples)
        reports[ep.key] = report
        label = ep.label if not ep.third_party else f"{ep.method} {ep.host}{ep.path_template}"
        console.print(f"  [bold]{label}[/]")
        for h in report.hypotheses:
            style = RESULT_STYLE[h.result]
            console.print(f"    [{style}]{h.result:<12}[/] {h.text}  [dim]{h.evidence}[/]")
        if report.note:
            console.print(f"    [dim]{report.note}[/]")
    console.print(f"[dim]{replayer.requests_made} direct calls made[/]")
    return reports


def _apply(ep: Endpoint, report: ProbeReport) -> None:
    ep.replayable = report.replayable
    ep.auth_type = report.auth_type
    ep.pagination = report.pagination
    ep.required_headers = report.required_headers


def _pagination_label(p: dict[str, Any] | None) -> str:
    if not p:
        return "-"
    mark = "" if p.get("confirmed") else "?"
    return f"{p['style']}: {p['param']}{mark}"


def _print_summary(
    recorder: TrafficRecorder, endpoints: list[Endpoint], host: str, seconds: float, run_id: int
) -> None:
    console.print()
    if endpoints:
        console.print(_endpoint_table(endpoints, host))
    else:
        console.print(
            "[yellow]No JSON API requests captured.[/] The site may render on the server."
        )
    if recorder.dropped:
        noise = ", ".join(f"{k}: {v}" for k, v in recorder.dropped.most_common())
        console.print(f"[dim]Filtered out: {noise}[/]")
    console.print(
        f"[dim]Run #{run_id}: {len(recorder.captured)} requests -> "
        f"{len(endpoints)} endpoint{'' if len(endpoints) == 1 else 's'} in {seconds:.1f}s[/]"
    )


def _endpoint_table(endpoints: list[Endpoint], host: str) -> Table:
    table = Table(title=f"Endpoints ({len(endpoints)})")
    table.add_column("Endpoint")
    table.add_column("Parameters")
    table.add_column("Samples", justify="right")
    table.add_column("Callable")
    table.add_column("Auth")
    table.add_column("Paging")
    table.add_column("Response fields")
    for ep in endpoints:
        label = ep.label if ep.host == host else f"{ep.method} {ep.host}{ep.path_template}"
        params = [f"{p.name}: {p.type} (path)" for p in ep.path_params]
        params += [
            f"{p.name}: {p.type} ({p.kind}{', redacted' if p.sensitive else ''})"
            for p in ep.query_params
        ]
        callable_ = {None: "-", True: "[green]yes[/]", False: "[red]no[/]"}[ep.replayable]
        auth = ep.auth_type or "-"
        if ep.required_headers:
            auth += "\n" + "\n".join(f"needs {h}" for h in ep.required_headers)
        table.add_row(
            label,
            "\n".join(params) or "-",
            str(ep.sample_count),
            callable_,
            auth,
            _pagination_label(ep.pagination),
            top_level_fields(ep.response_schema),
        )
    return table


class _Ping(BaseModel):
    status: str
    sum: int


@app.command()
def doctor() -> None:
    """Check that each LLM provider in .env answers (uses one tiny call per provider)."""
    table = Table(title="LLM providers")
    for col in ("Provider", "Model", "Key", "Result", "Time"):
        table.add_column(col)
    working = 0
    for provider in default_providers():
        if not provider.api_key:
            table.add_row(provider.name, provider.model, "[dim]missing[/]", "skipped", "-")
            continue
        gateway = LLMGateway([provider])
        started = time.monotonic()
        try:
            reply = gateway.ask(
                [{"role": "user", "content": 'What is 2 + 3? Set "status" to "ok".'}],
                response_model=_Ping,
            )
            good = reply.parsed is not None and reply.parsed.sum == 5
            result = "[green]OK[/]" if good else f"[yellow]odd answer:[/] {reply.text[:60]}"
            working += good
        except LLMError as exc:
            result = f"[red]failed:[/] {str(exc)[:90]}"
        finally:
            gateway.close()
        table.add_row(
            provider.name, provider.model, "set", result, f"{time.monotonic() - started:.1f}s"
        )
    console.print(table)
    if not working:
        console.print("[red]No provider is working.[/] Check the keys in .env.")
        raise typer.Exit(code=1)
    console.print(f"{working} provider(s) working. Fallback order: GHOST_API_PROVIDERS in .env.")


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
        endpoints = store.load_endpoints(domain) if domain else []
    if not runs:
        console.print("No runs found.")
        return
    table = Table(title="Runs")
    for col in ("ID", "Domain", "Goal", "Status", "Requests", "Started"):
        table.add_column(col)
    for r in runs:
        table.add_row(str(r.id), r.domain, r.goal, r.status, str(r.request_count), r.started_at)
    console.print(table)
    if domain:
        if endpoints:
            console.print(_endpoint_table(endpoints, domain))
        else:
            console.print("No endpoints stored for this domain.")
