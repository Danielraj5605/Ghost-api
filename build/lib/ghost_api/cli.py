"""Command-line entry point: `ghost-api run | refresh | generate | verify | report | inspect |
doctor`."""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from ghost_api import __version__
from ghost_api.agent.orchestrator import AgentResult, Explorer, StepRecord
from ghost_api.analysis.analyzer import analyze, endpoint_keys
from ghost_api.analysis.models import Endpoint
from ghost_api.analysis.schema import top_level_fields
from ghost_api.browser.driver import BrowserSession
from ghost_api.capture.models import CapturedRequest
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.config import Settings, default_db_path, load_env
from ghost_api.errors import friendly_error
from ghost_api.export import write_exports
from ghost_api.generator import (
    GenerationResult,
    NothingToGenerate,
    generate_client,
    generate_from,
    plan_for,
)
from ghost_api.generator.python_gen import GenerationError
from ghost_api.llm.gateway import LLMError, LLMGateway
from ghost_api.llm.providers import default_providers
from ghost_api.memory.knowledge import (
    DomainMemory,
    discovery_lessons,
    load_memory,
    memory_prompt,
    rebuild_raw,
    write_lessons,
)
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter, RobotsPolicy, fetch_robots
from ghost_api.probe.prober import Prober, ProbeReport
from ghost_api.probe.replay import Replayer, is_replayable
from ghost_api.verify.verifier import Round, Verifier

app = typer.Typer(
    help="Ghost API: turn any website into a typed, tested API client.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# With --no-agent, `run` just scrolls to the bottom this many times.
AUTO_SCROLLS = 5
# Agent steps allowed when verification sends the agent back to the site.
REPLAN_STEPS = 6
# Only runs with at least this many agent steps are worth an LLM call for lessons.
MIN_STEPS_FOR_LESSONS = 2


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
    no_generate: Annotated[
        bool, typer.Option("--no-generate", help="Don't generate a Python client.")
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

    host = urlsplit(settings.url).hostname or ""
    limiter = RateLimiter(settings.rate_limit)
    robots = _robots(settings.url, settings.ignore_robots, limiter)  # workflow stage 0
    gateway = None if no_agent else _gateway(settings.max_llm_calls, required=True)

    console.print(f"[bold]Goal:[/] {settings.goal}")
    console.print("[bold]Exploring site...[/]")
    started = time.monotonic()
    status, reason = "failed", ""
    with Store(settings.db_path) as store:
        s = _open_session(
            store,
            host,
            settings.url,
            settings.goal,
            settings.headed,
            limiter,
            robots,
            gateway,
            max_probes,
        )
        known = load_memory(store, host)
        if known is not None:
            s.memory = memory_prompt(known.endpoints, known.lessons)
            console.print(
                f"[dim]Using memory: {len(known.endpoints)} known endpoints, "
                f"{len(known.lessons)} lessons[/]"
            )
        try:
            if gateway is None:
                with BrowserSession(settings.url, s.recorder, headed=settings.headed) as browser:
                    browser.goto(settings.url)
                    for _ in range(AUTO_SCROLLS):
                        browser.scroll("bottom")
                status, reason = "captured", "--no-agent: scrolled the page"
            else:
                ask_user = _ask_user if settings.headed and sys.stdin.isatty() else None
                result = s.explore(
                    settings.goal, settings.max_steps, settings.max_minutes, ask_user=ask_user
                )
                status, reason = result.status, result.reason
        except KeyboardInterrupt:
            status, reason = "interrupted", "stopped by user"
        except Exception as exc:
            console.print(f"[red]Error:[/] {friendly_error(exc)}")
            reason = friendly_error(exc)
        _finish(
            s,
            status,
            reason,
            started,
            no_probe=no_probe,
            no_generate=no_generate,
            no_verify=no_verify,
            out=out,
        )
        console.print(f"[dim]Saved to {settings.db_path}[/]")
    if status in ("failed", "llm-unavailable"):
        raise typer.Exit(code=1)


# --- shared by `run` and `refresh` -----------------------------------------------------------


def _robots(url: str, ignore: bool, limiter: RateLimiter) -> RobotsPolicy:
    """Workflow stage 0: robots.txt. Exits if the start page is disallowed."""
    host = urlsplit(url).hostname or ""
    if ignore:
        console.print("[yellow]![/] robots.txt check skipped (--ignore-robots)")
        return RobotsPolicy.allow_all(host)
    robots = fetch_robots(url, limiter)
    verdict = robots.verdict(url)
    if not verdict.allowed:
        console.print(f"[red]Stopped:[/] {verdict.reason} for {url}")
        console.print("Use --ignore-robots only if you have permission to access this site.")
        raise typer.Exit(code=2)
    console.print(f"[green]OK[/] {verdict.reason}")
    if verdict.crawl_delay:
        limiter.slow_down_to(verdict.crawl_delay)
        console.print(f"   honoring Crawl-delay of {verdict.crawl_delay}s")
    return robots


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


@dataclass
class _Session:
    """Everything one `run` or `refresh` shares."""

    store: Store
    run_id: int
    host: str
    start_url: str
    goal: str
    headed: bool
    robots: RobotsPolicy
    recorder: TrafficRecorder
    replayer: Replayer
    gateway: LLMGateway | None
    memory: str = ""
    steps: list[StepRecord] = field(default_factory=list)

    def on_step(self, rec: StepRecord) -> None:
        self.steps.append(rec)
        self.store.add_step(
            self.run_id,
            rec.step,
            rec.thought,
            rec.action,
            rec.args,
            rec.result,
            len(rec.new_endpoints),
        )
        args = ", ".join(f"{k}={v!r}" for k, v in rec.args.items())
        console.print(f"[bold]Step {rec.step}[/] {rec.action}({args})  [dim]{rec.thought}[/]")
        for label in rec.new_endpoints:
            console.print(f"   [green]+ new endpoint[/] {label}")
        if rec.result.startswith(("rejected", "failed")):
            console.print(f"   [yellow]{rec.result}[/]")

    def explore(
        self,
        goal: str,
        max_steps: int,
        max_minutes: float,
        ask_user: Callable[[str], str] | None = None,
    ) -> AgentResult:
        """Open the browser and let the agent work toward `goal`."""
        assert self.gateway is not None
        with BrowserSession(self.start_url, self.recorder, headed=self.headed) as browser:
            return Explorer(
                browser=browser,
                llm=self.gateway,
                captured=lambda: self.recorder.captured,
                goal=goal,
                start_url=self.start_url,
                target_host=self.host,
                max_steps=max_steps,
                max_minutes=max_minutes,
                robots=self.robots,
                on_step=self.on_step,
                replayer=self.replayer,
                raw_requests=lambda: self.recorder.raw,
                add_sample=self.recorder.add,
                ask_user=ask_user,
                memory=self.memory,
            ).run()

    def replan(self, sub_goal: str) -> None:
        """Verification sends the agent back with a focused sub-goal (e.g. fresh session)."""
        if self.gateway is None:
            return
        console.print(f"[bold]Replanning:[/] {sub_goal}")
        with contextlib.suppress(Exception):
            self.explore(sub_goal, REPLAN_STEPS, 3)


def _open_session(
    store: Store,
    host: str,
    start_url: str,
    goal: str,
    headed: bool,
    limiter: RateLimiter,
    robots: RobotsPolicy,
    gateway: LLMGateway | None,
    max_probes: int,
) -> _Session:
    run_id = store.create_run(host, start_url, goal)
    if gateway is not None:
        gateway.store = store  # cache LLM answers in the same database

    def on_capture(req: CapturedRequest) -> None:
        store.add_request(run_id, req)
        tag = "third-party " if req.third_party else ""
        console.print(f"      [dim cyan]{tag}{req.method} {req.status} {_short(req.url, 70)}[/]")

    recorder = TrafficRecorder(host, on_capture=on_capture)
    replayer = Replayer(host, limiter, robots, max_requests=max_probes)
    return _Session(
        store, run_id, host, start_url, goal, headed, robots, recorder, replayer, gateway
    )


def _finish(
    s: _Session,
    status: str,
    reason: str,
    started: float,
    *,
    no_probe: bool,
    no_generate: bool,
    no_verify: bool,
    out: Path,
) -> None:
    """Workflow stages 4-8: probe, generate, verify, learn, report."""
    store, host = s.store, s.host
    stopped = status in ("interrupted", "bot-protection", "failed")

    reports: dict[str, ProbeReport] = {}
    if not no_probe and not stopped:
        reports = _probe_all(s.recorder, s.replayer)

    endpoints = analyze(s.recorder.captured)
    store.save_endpoints(host, s.run_id, endpoints)
    for ep in endpoints:
        report = reports.get(ep.key)
        if report is not None:
            _apply(ep, report)
            store.save_probe_report(host, s.run_id, ep, report)

    generated: GenerationResult | None = None
    generation_error = ""
    if not no_generate and endpoints and status not in ("interrupted", "bot-protection"):
        plan = plan_for(store, s.gateway, host, endpoints)
        try:
            if no_verify:
                generated = generate_from(
                    host, s.start_url, endpoints, s.recorder.captured, out, plan
                )
            else:
                console.print()
                console.print("[bold]Verifying the generated client against the live API...[/]")
                verifier = Verifier(
                    host,
                    s.start_url,
                    endpoints,
                    s.recorder.captured,
                    out,
                    raw=s.recorder.raw,
                    plan=plan,
                    replayer=s.replayer,
                    explore=s.replan if s.gateway is not None else None,
                    on_round=_print_round,
                    add_sample=s.recorder.add,
                )
                verification = verifier.run()
                generated = verification.result
                endpoints = verifier.endpoints
                store.save_endpoints(host, s.run_id, endpoints)  # e.g. relaxed schemas
                for ep in endpoints:
                    state, note = verification.statuses.get(ep.key, ("unverified", ""))
                    ep.status, ep.status_note = state, note
                    store.set_endpoint_status(host, ep, state, note)
        except (NothingToGenerate, GenerationError) as exc:
            generation_error = str(exc)
    s.replayer.close()

    # Workflow stage 8: remember what was learned.
    auto = discovery_lessons([(r.action, r.args, r.new_endpoints) for r in s.steps])
    learned = store.add_lessons(host, s.run_id, auto, "auto")
    if s.gateway is not None and len(s.steps) >= MIN_STEPS_FOR_LESSONS:
        texts = write_lessons(
            s.gateway, s.goal, status, [(r.action, r.args, r.result) for r in s.steps], endpoints
        )
        learned += store.add_lessons(host, s.run_id, texts, "llm")
    store.finish_run(s.run_id, status)
    if s.gateway is not None:
        store.record_usage(s.run_id, s.gateway.usage.answered, s.gateway.usage.tokens)
    exports = write_exports(store, host, s.run_id, out, generated.spec if generated else None)

    console.print()
    console.print(f"[bold]Finished:[/] {status} ({reason})")
    _print_summary(s.recorder, endpoints, host, time.monotonic() - started, s.run_id)
    if generated is not None:
        _print_generated(generated)
    elif generation_error:
        console.print(f"[yellow]No client generated:[/] {generation_error}")
    if learned:
        console.print(f"[dim]Remembered {learned} new lesson(s) for next time[/]")
    console.print(f"[dim]Report: {exports.report}   OpenAPI: {exports.openapi}[/]")
    if s.gateway is not None:
        s.gateway.close()
        u = s.gateway.usage
        providers = ", ".join(f"{k}: {v}" for k, v in u.by_provider.items()) or "-"
        console.print(
            f"[dim]LLM: {u.answered} answered calls ({providers}), {u.tokens:,} tokens, "
            f"{u.cache_hits} cached, {u.retries} retries, {u.repairs} repairs, "
            f"{u.fallbacks} fallbacks[/]"
        )
        for problem, count in u.problems.most_common(5):
            console.print(f"[dim]   {count}x {problem}[/]")


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


STATUS_STYLE = {"verified": "green", "unverified": "yellow", "failed": "red"}
OUTCOME_STYLE = {"passed": "green", "failed": "red", "skipped": "yellow"}


def _print_round(round_: Round) -> None:
    counts = {k: sum(o.outcome == k for o in round_.outcomes) for k in OUTCOME_STYLE}
    summary = ", ".join(f"[{OUTCOME_STYLE[k]}]{v} {k}[/]" for k, v in counts.items() if v)
    console.print(f"  [bold]Round {round_.number}:[/] {summary or 'no tests'}")
    for o in round_.outcomes:
        if o.outcome == "failed":
            first = next((ln for ln in o.message.splitlines() if ln.strip()), "")
            console.print(f"    [red]failed[/] {o.name}  [dim]{first[:110]}[/]")
    for r in round_.repairs:
        console.print(f"    [cyan]repair[/] {r.method} ({r.kind}): {r.action}")


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
    table.add_column("Verified")
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
        style = STATUS_STYLE.get(ep.status)
        verified = f"[{style}]{ep.status}[/]" if style else "-"
        if ep.status_note and style:
            verified += f"\n[dim]{ep.status_note}[/]"
        table.add_row(
            label,
            "\n".join(params) or "-",
            str(ep.sample_count),
            verified,
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
def generate(
    domain: Annotated[str, typer.Argument(help="Domain explored before, e.g. quotes.toscrape.com")],
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Use names derived from URLs (no LLM call).")
    ] = False,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """(Re)generate the Python client for a site from its stored endpoint map."""
    path = db or default_db_path()
    if not path.exists():
        console.print(f"No database at {path}. Run `ghost-api run` first.")
        raise typer.Exit(code=1)
    gateway = None if no_llm else LLMGateway(default_providers(), max_calls=3)
    if gateway is not None and not gateway.providers:
        gateway = None
    try:
        with Store(path) as store:
            if gateway is not None:
                gateway.store = store
            result = generate_client(store, domain, out, llm=gateway)
    except (NothingToGenerate, GenerationError) as exc:
        console.print(f"[red]No client generated:[/] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        if gateway is not None:
            gateway.close()
    _print_generated(result)


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


def _print_generated(result: GenerationResult) -> None:
    spec, client = result.spec, result.client
    how = "names chosen by the LLM" if result.named_by_llm else "names derived from URLs"
    console.print(f"\n[bold green]Generated[/] {spec.class_name} ({how}) in {client.directory}")
    for m in spec.methods:
        args = ", ".join(p.name for p in m.required)
        extra = f"  + iter_{m.name}()" if m.page_param else ""
        console.print(f"   {m.name}({args}) -> {m.response_type}{extra}")
    for c in spec.credentials:
        console.print(f"   [yellow]needs[/] {c.arg} (set {c.env} or pass it to the client)")
    for skipped in spec.skipped:
        console.print(f"   [dim]skipped {skipped}[/]")
    console.print(
        f"[dim]Files: {client.client_path.name}, {client.models_path.name}, "
        f"{client.tests_path.name}[/]"
    )


@app.command()
def verify(
    domain: Annotated[str, typer.Argument(help="Domain explored before, e.g. quotes.toscrape.com")],
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Regenerate a site's client, run its tests against the live API, and repair.

    Uses stored data only. Endpoints that need a key are tested when the key is set in
    the environment (the generated client says which variable).
    """
    path = db or default_db_path()
    if not path.exists():
        console.print(f"No database at {path}. Run `ghost-api run` first.")
        raise typer.Exit(code=1)
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
            on_round=_print_round,
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
    console.print(_endpoint_table(verifier.endpoints, domain))
    if report.result is not None:
        _print_generated(report.result)
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
    """Write report.md and openapi.yaml for a site from what is stored."""
    path = db or default_db_path()
    if not path.exists():
        console.print(f"No database at {path}. Run `ghost-api run` first.")
        raise typer.Exit(code=1)
    with Store(path) as store:
        run_row = store.get_run(run_id) if run_id else store.latest_run(domain)
        if run_row is None or run_row.domain != domain:
            console.print(f"No run found for {domain}.")
            raise typer.Exit(code=1)
        exports = write_exports(store, domain, run_row.id, out)
    console.print(f"Report:  {exports.report}")
    console.print(f"OpenAPI: {exports.openapi}")


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
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Re-check a known site using memory: replay first, browse only if needed.

    Also continues an interrupted run, since its traffic and endpoints were saved.
    """
    path = db or default_db_path()
    if not path.exists():
        console.print(f"No database at {path}. Run `ghost-api run` first.")
        raise typer.Exit(code=1)
    with Store(path) as store:
        mem = load_memory(store, domain)
        if mem is None:
            console.print(f"Nothing remembered for {domain}. Run `ghost-api run` first.")
            raise typer.Exit(code=1)
        console.print(
            f"[bold]Refreshing {domain}[/] from memory: {len(mem.endpoints)} "
            f"endpoint(s), {len(mem.lessons)} lesson(s)"
        )
        limiter = RateLimiter(rate_limit)
        robots = _robots(mem.start_url, ignore_robots, limiter)
        gateway = None if no_llm else _gateway(max_llm_calls, required=False)
        started = time.monotonic()
        s = _open_session(
            store,
            domain,
            mem.start_url,
            f"refresh: {mem.goal}",
            headed,
            limiter,
            robots,
            gateway,
            max_probes,
        )
        s.memory = memory_prompt(mem.endpoints, mem.lessons)
        status, reason = "refreshed", ""
        try:
            # 1. Stored samples that can be replayed as they are: no browser, no LLM.
            _preload(s, mem)
            missing = _missing(s, mem)
            console.print(
                f"[bold]1.[/] {len(mem.endpoints) - len(missing)} of "
                f"{len(mem.endpoints)} endpoint(s) can be re-checked from stored "
                "samples (no browser, no LLM)"
            )
            # 2. One plain visit for fresh traffic (fresh keys, cookies): still no LLM.
            if missing:
                labels = ", ".join(e.label for e in missing)
                console.print(f"[bold]2.[/] Visiting the site once for fresh traffic: {labels}")
                with BrowserSession(mem.start_url, s.recorder, headed=headed) as browser:
                    browser.goto(mem.start_url)
                    browser.scroll("bottom")
                missing = _missing(s, mem)
            # 3. Only what's still missing needs the agent.
            if missing and gateway is not None:
                labels = ", ".join(e.label for e in missing)
                console.print(f"[bold]3.[/] The agent looks for: {labels}")
                goal = f"{mem.goal}. Make the site send these requests again: {labels}"
                result = s.explore(goal, max_steps, 5)
                if result.status in ("bot-protection", "interrupted"):
                    status = result.status
                missing = _missing(s, mem)
            reason = (
                f"{len(missing)} known endpoint(s) not seen again: "
                + ", ".join(e.label for e in missing)
                if missing
                else "all known endpoints re-captured"
            )
        except KeyboardInterrupt:
            status, reason = "interrupted", "stopped by user"
        except Exception as exc:
            console.print(f"[red]Error:[/] {friendly_error(exc)}")
            reason = friendly_error(exc)
        _finish(
            s,
            status,
            reason,
            started,
            no_probe=False,
            no_generate=False,
            no_verify=no_verify,
            out=out,
        )
        first = store.first_run_llm_calls(domain)
        now = gateway.usage.answered if gateway is not None else 0
        if first:
            saved = 100 * (first - now) / first
            console.print(
                f"[bold]Memory:[/] {now} LLM call(s) vs {first} on the first run "
                f"({saved:.0f}% fewer)"
            )


def _preload(s: _Session, mem: DomainMemory, per_endpoint: int = 5) -> None:
    """Load each known endpoint's latest stored samples, rebuilt for replay where possible.

    They go straight into the recorder's lists (not re-saved: they're already stored).
    """
    stored = s.store.requests_for_domain(mem.domain)
    known = {e.key: e for e in mem.endpoints}
    picked: dict[str, list[CapturedRequest]] = {}
    for key, req in zip(endpoint_keys(stored), stored, strict=False):
        if key in known:
            picked.setdefault(key, []).append(req)
    for key, reqs in picked.items():
        for req in reqs[-per_endpoint:]:
            s.recorder.captured.append(req)
            s.recorder.raw.append(rebuild_raw(req, known[key].required_headers))


def _missing(s: _Session, mem: DomainMemory) -> list[Endpoint]:
    """Known endpoints with no sample that can be replayed right now."""
    ready = {
        key
        for key, raw in zip(endpoint_keys(s.recorder.captured), s.recorder.raw, strict=False)
        if raw is not None and is_replayable(raw)
    }
    return [e for e in mem.endpoints if e.key not in ready]


@app.command()
def inspect(
    domain: Annotated[str | None, typer.Argument(help="Domain to show; omit for all.")] = None,
    run_id: Annotated[
        int | None, typer.Option("--run", help="Replay one run's step log and test results.")
    ] = None,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Show what's stored for a site, or replay one run with --run."""
    path = db or default_db_path()
    if not path.exists():
        console.print(f"No database at {path}. Run `ghost-api run` first.")
        raise typer.Exit(code=1)
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
            console.print(_endpoint_table(endpoints, domain))
        else:
            console.print("No endpoints stored for this domain.")
        if lessons:
            console.print("[bold]Lessons for next time[/] (newest first):")
            for lesson in lessons:
                console.print(f"  - {lesson}")
