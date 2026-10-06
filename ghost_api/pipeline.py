"""The Ghost API workflow, with no user interface.

Everything here emits events (`ghost_api/events.py`) instead of printing, so the terminal,
the web dashboard and the database all see the same run. Every event is also saved in
the `run_events` table, so any run can be replayed later.

Workflow: robots.txt check -> explore (agent or plain visit) -> test endpoints directly ->
generate the client -> verify it against the live API (repairing failures) -> remember
lessons -> write report.md and openapi.yaml.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ghost_api.agent.orchestrator import AgentResult, Explorer, StepRecord
from ghost_api.analysis.analyzer import analyze, endpoint_keys
from ghost_api.analysis.models import Endpoint
from ghost_api.analysis.schema import field_paths, top_level_fields
from ghost_api.browser.driver import BrowserSession
from ghost_api.capture.models import CapturedRequest
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.errors import friendly_error
from ghost_api.events import (
    AgentStep,
    ClientGenerated,
    EndpointsUpdated,
    Event,
    EventBus,
    HypothesisTested,
    Notice,
    Phase,
    PhaseChanged,
    ProbeFinished,
    RequestCaptured,
    RunFinished,
    RunStarted,
    Screenshot,
    UsageUpdated,
    VerifyRound,
)
from ghost_api.export import write_exports
from ghost_api.export.confidence import confidence
from ghost_api.generator import GenerationResult, NothingToGenerate, generate_from, plan_for
from ghost_api.generator.python_gen import GenerationError
from ghost_api.generator.ts_gen import camel, ts_type
from ghost_api.llm.gateway import LLMGateway
from ghost_api.memory.knowledge import (
    DomainMemory,
    discovery_lessons,
    rebuild_raw,
    write_lessons,
)
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter, RobotsPolicy, fetch_robots
from ghost_api.probe.prober import Prober, ProbeReport
from ghost_api.probe.replay import Replayer, is_replayable
from ghost_api.verify.verifier import Round, Verifier

# With no agent, the page is just loaded and scrolled to the bottom this many times.
AUTO_SCROLLS = 5
# Agent steps allowed when verification sends the agent back to the site.
REPLAN_STEPS = 6
# Only runs with at least this many agent steps are worth an LLM call for lessons.
MIN_STEPS_FOR_LESSONS = 2
# Runs that ended this way are not probed or turned into a client.
STOPPED = ("interrupted", "bot-protection", "failed")


@dataclass
class RunOptions:
    lang: str = "python"
    headed: bool = False
    max_steps: int = 40
    max_minutes: float = 15.0
    max_probes: int = 60
    no_probe: bool = False
    no_generate: bool = False
    no_verify: bool = False
    out: Path = Path("output")


def screenshots_dir(db_path: Path, run_id: int) -> Path:
    """Where a run's browser screenshots go: next to the database, never in the output."""
    return db_path.parent / f"{db_path.stem}_files" / "runs" / str(run_id)


# --- stage 0: robots.txt ---------------------------------------------------------------------


def robots_policy(
    url: str, ignore: bool, limiter: RateLimiter, bus: EventBus
) -> tuple[RobotsPolicy, bool]:
    """The site's robots.txt rules, and whether the start page may be visited."""
    host = urlsplit(url).hostname or ""
    if ignore:
        bus.emit(Notice("robots.txt check skipped (--ignore-robots)", "warning"))
        return RobotsPolicy.allow_all(host), True
    robots = fetch_robots(url, limiter)
    verdict = robots.verdict(url)
    if not verdict.allowed:
        bus.emit(
            Notice(
                f"Stopped: {verdict.reason} for {url}",
                "error",
                "Use --ignore-robots only if you have permission to access this site.",
            )
        )
        return robots, False
    bus.emit(Notice(f"robots.txt: {verdict.reason}", "success"))
    if verdict.crawl_delay:
        limiter.slow_down_to(verdict.crawl_delay)
        bus.emit(Notice(f"Honoring the site's Crawl-delay of {verdict.crawl_delay}s"))
    return robots, True


# --- the session -----------------------------------------------------------------------------


@dataclass
class Session:
    """Everything one run or refresh shares."""

    store: Store
    bus: EventBus
    run_id: int
    host: str
    start_url: str
    goal: str
    headed: bool
    robots: RobotsPolicy
    recorder: TrafficRecorder
    replayer: Replayer
    gateway: LLMGateway | None
    screens: Path | None = None
    stop: Callable[[], bool] | None = None  # e.g. the dashboard's Stop button
    memory: str = ""
    steps: list[StepRecord] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        self.bus.emit(event)

    def phase(self, phase: Phase) -> None:
        self.emit(PhaseChanged(phase))

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
        self.emit(
            AgentStep(
                step=rec.step,
                action=rec.action,
                args=dict(rec.args),
                thought=rec.thought,
                result=rec.result,
                new_endpoints=list(rec.new_endpoints),
                requests=rec.requests,
                screenshot=rec.screenshot,
                box=rec.box,
            )
        )
        if self.gateway is not None:
            self.emit(usage_event(self.gateway))

    def _shooter(self, browser: BrowserSession) -> Callable[[str], str | None] | None:
        if self.screens is None:
            return None
        folder = self.screens

        def shoot(name: str) -> str | None:
            file = f"{name}.jpg"
            return file if browser.screenshot(folder / file) else None

        return shoot

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
                screenshot=self._shooter(browser),
                should_stop=self.stop,
            ).run()

    def visit(self, scrolls: int) -> None:
        """Load the start page and scroll, without the agent (no LLM)."""
        with BrowserSession(self.start_url, self.recorder, headed=self.headed) as browser:
            browser.goto(self.start_url)
            for _ in range(scrolls):
                browser.scroll("bottom")
            shoot = self._shooter(browser)
            name = shoot("visit") if shoot else None
            if name:
                self.emit(Screenshot(name, browser.url))

    def replan(self, sub_goal: str) -> None:
        """Verification sends the agent back with a focused sub-goal (e.g. fresh session)."""
        if self.gateway is None:
            return
        self.emit(Notice(f"Replanning: {sub_goal}"))
        with contextlib.suppress(Exception):
            self.explore(sub_goal, REPLAN_STEPS, 3)


def open_session(
    store: Store,
    bus: EventBus,
    host: str,
    start_url: str,
    goal: str,
    headed: bool,
    limiter: RateLimiter,
    robots: RobotsPolicy,
    gateway: LLMGateway | None,
    max_probes: int,
    screens_root: Path | None = None,
    mode: str = "run",
    lang: str = "python",
) -> Session:
    """Create the run in the database and start recording its events."""
    run_id = store.create_run(host, start_url, goal)
    bus.subscribe(lambda event: store.add_event(run_id, event.to_dict()))
    if gateway is not None:
        gateway.store = store  # cache LLM answers in the same database

    def on_capture(req: CapturedRequest) -> None:
        store.add_request(run_id, req)
        bus.emit(RequestCaptured(req.method, req.url, req.status, req.third_party))

    recorder = TrafficRecorder(host, on_capture=on_capture)
    replayer = Replayer(host, limiter, robots, max_requests=max_probes)
    screens = screenshots_dir(screens_root, run_id) if screens_root is not None else None
    session = Session(
        store,
        bus,
        run_id,
        host,
        start_url,
        goal,
        headed,
        robots,
        recorder,
        replayer,
        gateway,
        screens,
    )
    bus.emit(
        RunStarted(run_id, host, start_url, goal, "refresh" if mode == "refresh" else "run", lang)
    )
    return session


# --- run and refresh -------------------------------------------------------------------------


def explore_site(
    s: Session,
    opts: RunOptions,
    use_agent: bool,
    ask_user: Callable[[str], str] | None = None,
) -> tuple[str, str]:
    """Workflow stage 1: explore. Returns (status, reason)."""
    s.phase("explore")
    try:
        if not use_agent:
            s.visit(AUTO_SCROLLS)
            return "captured", "--no-agent: scrolled the page"
        result = s.explore(s.goal, opts.max_steps, opts.max_minutes, ask_user=ask_user)
        return result.status, result.reason
    except KeyboardInterrupt:
        return "interrupted", "stopped by user"
    except Exception as exc:
        s.emit(Notice(friendly_error(exc), "error"))
        return "failed", friendly_error(exc)


def refresh_site(s: Session, mem: DomainMemory, max_steps: int) -> tuple[str, str]:
    """Re-check known endpoints with as little work as possible. Returns (status, reason)."""
    status = "refreshed"
    s.phase("explore")
    try:
        # 1. Stored samples that can be replayed as they are: no browser, no LLM.
        preload(s, mem)
        missing = missing_endpoints(s, mem)
        s.emit(
            Notice(
                f"1. {len(mem.endpoints) - len(missing)} of {len(mem.endpoints)} endpoint(s) can "
                "be re-checked from stored samples (no browser, no LLM)"
            )
        )
        # 2. One plain visit for fresh traffic (fresh keys, cookies): still no LLM.
        if missing:
            labels = ", ".join(e.label for e in missing)
            s.emit(Notice(f"2. Visiting the site once for fresh traffic: {labels}"))
            s.visit(1)
            missing = missing_endpoints(s, mem)
        # 3. Only what's still missing needs the agent.
        if missing and s.gateway is not None:
            labels = ", ".join(e.label for e in missing)
            s.emit(Notice(f"3. The agent looks for: {labels}"))
            goal = f"{mem.goal}. Make the site send these requests again: {labels}"
            result = s.explore(goal, max_steps, 5)
            if result.status in ("bot-protection", "interrupted"):
                status = result.status
            missing = missing_endpoints(s, mem)
    except KeyboardInterrupt:
        return "interrupted", "stopped by user"
    except Exception as exc:
        s.emit(Notice(friendly_error(exc), "error"))
        return status, friendly_error(exc)
    if missing:
        names = ", ".join(e.label for e in missing)
        return status, f"{len(missing)} known endpoint(s) not seen again: {names}"
    return status, "all known endpoints re-captured"


def preload(s: Session, mem: DomainMemory, per_endpoint: int = 5) -> None:
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


def missing_endpoints(s: Session, mem: DomainMemory) -> list[Endpoint]:
    """Known endpoints with no sample that can be replayed right now."""
    ready = {
        key
        for key, raw in zip(endpoint_keys(s.recorder.captured), s.recorder.raw, strict=False)
        if raw is not None and is_replayable(raw)
    }
    return [e for e in mem.endpoints if e.key not in ready]


# --- stages 2-8: test, generate, verify, learn, report ---------------------------------------


@dataclass
class FinishResult:
    endpoints: list[Endpoint]
    generated: GenerationResult | None
    generation_error: str


def finish(s: Session, status: str, reason: str, started: float, opts: RunOptions) -> FinishResult:
    store, host = s.store, s.host
    reports: dict[str, ProbeReport] = {}
    if not opts.no_probe and status not in STOPPED:
        s.phase("test")
        reports = probe_all(s)

    endpoints = analyze(s.recorder.captured)
    store.save_endpoints(host, s.run_id, endpoints)
    for ep in endpoints:
        report = reports.get(ep.key)
        if report is not None:
            apply_probe(ep, report)
            store.save_probe_report(host, s.run_id, ep, report)
    s.emit(EndpointsUpdated([endpoint_summary(e, host) for e in endpoints]))

    generated: GenerationResult | None = None
    generation_error = ""
    if not opts.no_generate and endpoints and status not in ("interrupted", "bot-protection"):
        s.phase("generate")
        plan = plan_for(store, s.gateway, host, endpoints)
        try:
            if opts.no_verify:
                generated = generate_from(
                    host, s.start_url, endpoints, s.recorder.captured, opts.out, plan, opts.lang
                )
            else:
                s.phase("verify")
                s.emit(Notice("Verifying the generated client against the live API..."))
                verifier = Verifier(
                    host,
                    s.start_url,
                    endpoints,
                    s.recorder.captured,
                    opts.out,
                    raw=s.recorder.raw,
                    plan=plan,
                    replayer=s.replayer,
                    explore=s.replan if s.gateway is not None else None,
                    on_round=lambda r: s.emit(round_event(r)),
                    add_sample=s.recorder.add,
                    lang=opts.lang,
                )
                verification = verifier.run()
                generated = verification.result
                endpoints = verifier.endpoints
                store.save_endpoints(host, s.run_id, endpoints)  # e.g. relaxed schemas
                for ep in endpoints:
                    state, note = verification.statuses.get(ep.key, ("unverified", ""))
                    ep.status, ep.status_note = state, note
                    store.set_endpoint_status(host, ep, state, note)
                s.emit(EndpointsUpdated([endpoint_summary(e, host) for e in endpoints]))
        except (NothingToGenerate, GenerationError) as exc:
            generation_error = str(exc)
            s.emit(Notice(f"No client generated: {exc}", "warning"))
    s.replayer.close()
    if generated is not None:
        s.emit(client_event(generated))

    # Workflow stage 8: remember what was learned.
    s.phase("learn")
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
        s.emit(usage_event(s.gateway))
        s.gateway.close()
    exports = write_exports(store, host, s.run_id, opts.out, generated.spec if generated else None)

    s.emit(
        RunFinished(
            status=status,
            reason=reason,
            seconds=round(time.monotonic() - started, 1),
            requests=len(s.recorder.captured),
            endpoints=len(endpoints),
            report=str(exports.report),
            openapi=str(exports.openapi),
            lessons_learned=learned,
            filtered=dict(s.recorder.dropped),
            generation_error=generation_error,
        )
    )
    s.phase("done")
    return FinishResult(endpoints, generated, generation_error)


def probe_all(s: Session) -> dict[str, ProbeReport]:
    """Workflow stage 4: test every endpoint with direct calls."""
    recorder = s.recorder
    endpoints = analyze(recorder.captured)
    if not endpoints:
        return {}
    s.emit(Notice(f"Testing {len(endpoints)} endpoint(s) with direct calls..."))
    keys = endpoint_keys(recorder.captured)
    raws = list(recorder.raw)
    prober = Prober(s.replayer, on_sample=recorder.add)
    reports: dict[str, ProbeReport] = {}
    for ep in endpoints:
        samples = [r for k, r in zip(keys, raws, strict=False) if k == ep.key and r]
        report = prober.probe(ep, samples)
        reports[ep.key] = report
        label = display_label(ep, s.host)
        for h in report.hypotheses:
            s.emit(HypothesisTested(label, h.text, h.result, h.evidence))
        if report.note:
            s.emit(Notice(f"{label}: {report.note}"))
    s.emit(ProbeFinished(len(endpoints), s.replayer.requests_made))
    return reports


def apply_probe(ep: Endpoint, report: ProbeReport) -> None:
    ep.replayable = report.replayable
    ep.auth_type = report.auth_type
    ep.pagination = report.pagination
    ep.required_headers = report.required_headers


# --- event payloads --------------------------------------------------------------------------


def display_label(ep: Endpoint, host: str) -> str:
    return ep.label if ep.host == host else f"{ep.method} {ep.host}{ep.path_template}"


def endpoint_summary(ep: Endpoint, host: str) -> dict[str, Any]:
    """Everything an interface needs to show an endpoint, as plain JSON."""
    params = [
        {
            "name": p.name,
            "type": p.type,
            "location": "path",
            "kind": "required",
            "examples": p.examples[:3],
            "sensitive": False,
        }
        for p in ep.path_params
    ] + [
        {
            "name": p.name,
            "type": p.type,
            "location": "query",
            "kind": p.kind,
            "examples": p.examples[:3],
            "sensitive": p.sensitive,
        }
        for p in ep.query_params
    ]
    return {
        "key": ep.key,
        "label": display_label(ep, host),
        "method": ep.method,
        "host": ep.host,
        "path": ep.path_template,
        "third_party": ep.host != host,
        "params": params,
        "samples": ep.sample_count,
        "statuses": ep.statuses,
        "status": ep.status,
        "status_note": ep.status_note,
        "replayable": ep.replayable,
        "auth_type": ep.auth_type,
        "required_headers": ep.required_headers,
        "pagination": ep.pagination,
        "confidence": confidence(ep),
        "fields": top_level_fields(ep.response_schema),
        "field_paths": field_paths(ep.response_schema, limit=40),
        "has_request_body": ep.request_schema is not None,
    }


def round_event(r: Round) -> VerifyRound:
    counts = {k: sum(o.outcome == k for o in r.outcomes) for k in ("passed", "failed", "skipped")}
    failures = []
    for o in r.outcomes:
        if o.outcome == "failed":
            first = next((ln for ln in o.message.splitlines() if ln.strip()), "")
            failures.append({"name": o.name, "message": first[:200]})
    repairs = [{"method": x.method, "kind": x.kind, "action": x.action} for x in r.repairs]
    return VerifyRound(
        r.number, counts["passed"], counts["failed"], counts["skipped"], failures, repairs
    )


def client_event(result: GenerationResult) -> ClientGenerated:
    spec, client = result.spec, result.client
    typescript = client.client_path.suffix == ".ts"
    methods = []
    for m in spec.methods:
        if typescript:
            name = camel(m.name)
            args = [camel(p.name) for p in m.required]
            iterator = f"iter{name[:1].upper()}{name[1:]}" if m.page_param else None
            returns = ts_type(m.response_type)
        else:
            name, args = m.name, [p.name for p in m.required]
            iterator = f"iter_{m.name}" if m.page_param else None
            returns = m.response_type
        methods.append(
            {
                "name": name,
                "args": args,
                "returns": returns,
                "iterator": iterator,
                "summary": m.summary,
                "endpoint": m.endpoint_key,
            }
        )
    files = list(
        dict.fromkeys(p.name for p in (client.client_path, client.models_path, client.tests_path))
    )
    return ClientGenerated(
        class_name=spec.class_name,
        lang="typescript" if typescript else "python",
        named_by_llm=result.named_by_llm,
        directory=str(client.directory),
        methods=methods,
        credentials=[{"arg": c.arg, "env": c.env} for c in spec.credentials],
        skipped=list(spec.skipped),
        files=files,
    )


def usage_event(gateway: LLMGateway) -> UsageUpdated:
    u = gateway.usage
    return UsageUpdated(
        answered=u.answered,
        tokens=u.tokens,
        cache_hits=u.cache_hits,
        retries=u.retries,
        fallbacks=u.fallbacks,
        by_provider=dict(u.by_provider),
        problems=[f"{n}x {p}" for p, n in u.problems.most_common(5)],
    )
