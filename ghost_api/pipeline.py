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
from typing import Any, Literal
from urllib.parse import urlsplit

from ghost_api import drift
from ghost_api.agent.orchestrator import AgentResult, Explorer, StepRecord
from ghost_api.analysis.analyzer import analyze, endpoint_keys
from ghost_api.analysis.detectors import is_bot_challenge
from ghost_api.analysis.exposure import exposures, summary_line
from ghost_api.analysis.models import Endpoint
from ghost_api.audit import AuditLog
from ghost_api.browser import auth_state
from ghost_api.browser.driver import BrowserSession
from ghost_api.browser.gate import RequestGate
from ghost_api.capture.models import CapturedRequest
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.errors import error_code, friendly_error
from ghost_api.events import (
    AgentStep,
    ClientGenerated,
    EndpointsUpdated,
    Event,
    EventBus,
    HypothesisTested,
    NetworkSummary,
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
from ghost_api.export.site_data import display_label, endpoint_summary
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
from ghost_api.net import DenyPaths, MultiHostRobotsPolicy, RateLimiter, RobotsPolicy, fetch_robots
from ghost_api.netguard import NetworkPolicy
from ghost_api.probe.prober import Prober, ProbeReport
from ghost_api.probe.replay import Replayer, is_replayable
from ghost_api.project import site_policy
from ghost_api.site import site_key
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
    third_party_robots: bool = False  # also check other sites' robots.txt (browser calls)


def screenshots_dir(db_path: Path, run_id: int) -> Path:
    """Where a run's browser screenshots go: next to the database, never in the output."""
    return db_path.parent / f"{db_path.stem}_files" / "runs" / str(run_id)


# --- stage 0: robots.txt ---------------------------------------------------------------------


def robots_policy(
    url: str, ignore: bool, limiter: RateLimiter, bus: EventBus
) -> tuple[RobotsPolicy, bool]:
    """The site's robots.txt rules, and whether the start page may be visited.

    Returns a ``MultiHostRobotsPolicy`` that lazily fetches robots.txt for any
    new subdomain the agent navigates to (Issue 3).
    """
    host = urlsplit(url).hostname or ""
    policy, _ = site_policy(site_key(url) or host)
    robots: RobotsPolicy
    robots, allowed = _robots_rules(url, host, ignore, limiter, bus)
    if policy.deny_paths:
        bus.emit(Notice(f"Project rules: {len(policy.deny_paths)} path(s) are never requested "
                        f"({', '.join(policy.deny_paths[:4])})"))  # fmt: skip
        robots = DenyPaths(robots, policy.deny_paths)
        if not robots.allows(url):
            bus.emit(Notice(f"Stopped: {url} is denied by the project rules", "error"))
            return robots, False
    return robots, allowed


def _robots_rules(
    url: str, host: str, ignore: bool, limiter: RateLimiter, bus: EventBus
) -> tuple[MultiHostRobotsPolicy, bool]:
    if ignore:
        bus.emit(Notice("robots.txt check skipped (--ignore-robots)", "warning"))
        return (
            MultiHostRobotsPolicy(
                RobotsPolicy.allow_all(host),
                limiter,
                lambda u, _l: RobotsPolicy.allow_all(urlsplit(u).hostname or ""),
            ),
            True,
        )
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
        return MultiHostRobotsPolicy(robots, limiter), False
    bus.emit(Notice(f"robots.txt: {verdict.reason}", "success"))
    if verdict.crawl_delay:
        limiter.slow_down_to(verdict.crawl_delay)
        bus.emit(Notice(f"Honoring the site's Crawl-delay of {verdict.crawl_delay}s"))
    return MultiHostRobotsPolicy(robots, limiter), True


# --- the session -----------------------------------------------------------------------------


@dataclass
class Session:
    """Everything one run or refresh shares."""

    store: Store
    bus: EventBus
    run_id: int
    host: str  # the server: first-party vs third-party, robots.txt
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
    site: str = ""  # memory and output folder: host or host/section (ghost_api/site.py)
    network: NetworkPolicy | None = None  # where the browser may connect (netguard.py)
    login: dict[str, Any] | None = None  # a saved login for the site (auth_state.py)
    third_party_robots: bool = False  # RunOptions.third_party_robots
    audit: AuditLog | None = None  # every request sent or blocked (audit.py)
    _blocked: set[str] = field(default_factory=set)

    def emit(self, event: Event) -> None:
        self.bus.emit(event)

    def _on_block(self, message: str) -> None:
        """A request the network rules stopped: said once in the run log (and audited)."""
        if self.audit is not None:
            self.audit.blocked(message)
        if message not in self._blocked:
            self._blocked.add(message)
            self.emit(Notice(message, "warning"))

    def browser(self) -> BrowserSession:
        """A browser whose every request follows the run's rules: no private addresses
        (unless the target itself is local), robots.txt and the rate limit."""
        gate = RequestGate(
            self.host,
            self.robots,
            self.replayer.limiter,
            self._on_block,
            third_party_robots=self.third_party_robots,
        )
        return BrowserSession(
            self.start_url,
            self.recorder,
            headed=self.headed,
            policy=self.network,
            gate=gate,
            on_block=self._on_block,
            storage_state=self.login,
        )

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
            if not browser.screenshot(folder / file):
                return None
            # Step screenshots travel in their AgentStep event; announce the others (the
            # page as first loaded), or a run that finishes at step 1 shows no picture.
            if not name.startswith("step-"):
                self.emit(Screenshot(file, browser.url))
            return file

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
        with self.browser() as browser:
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

    def visit(self, scrolls: int) -> str | None:
        """Load the start page and scroll, without the agent (no LLM). Returns the page
        title when it is a bot-protection page (nothing more is done there), else None."""
        with self.browser() as browser:
            browser.goto(self.start_url)
            snapshot = browser.snapshot(self.host, self.robots)
            if is_bot_challenge(snapshot.title, snapshot.text):
                return snapshot.title  # never try to get past bot protection (NFR-19)
            for _ in range(scrolls):
                if self.stop is not None and self.stop():
                    break
                browser.scroll("bottom")
            shoot = self._shooter(browser)
            if shoot:
                shoot("visit")
        return None

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
    site: str | None = None,
) -> Session:
    """Create the run in the database and start recording its events.

    `site` defaults to the start URL's site; a refresh passes the name its memory is under."""
    site = site or site_key(start_url) or host
    run_id = store.create_run(site, start_url, goal)
    bus.subscribe(lambda event: store.add_event(run_id, event.to_dict()))
    if gateway is not None:
        gateway.store = store  # cache LLM answers in the same database

    def on_capture(req: CapturedRequest) -> None:
        store.add_request(run_id, req)
        bus.emit(RequestCaptured(req.method, req.url, req.status, req.third_party))

    recorder = TrafficRecorder(host, on_capture=on_capture)
    network = NetworkPolicy.for_target(start_url)
    replayer = Replayer(host, limiter, robots, max_requests=max_probes, policy=network)
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
        site=site,
        network=network,
        login=_saved_login(store, site, bus),
        audit=AuditLog(store.path, site, run_id),
    )
    audit = session.audit
    assert audit is not None
    replayer.on_sent = lambda method, url, status, decision: audit.record(
        "direct", method, url, status, decision
    )
    bus.emit(
        RunStarted(run_id, site, start_url, goal, "refresh" if mode == "refresh" else "run", lang)
    )
    if network.allow_private:
        bus.emit(Notice("Local target: this computer and the local network may be reached"))
    return session


def network_summary(s: Session) -> NetworkSummary:
    third_party = sorted({urlsplit(c.url).hostname or "" for c in s.recorder.captured
                          if c.third_party} - {""})  # fmt: skip
    return NetworkSummary(
        target=s.host,
        robots=s.robots.reason,
        rate_limit=round(1 / s.replayer.limiter.interval, 2) if s.replayer.limiter.interval else 0,
        local_target=bool(s.network and s.network.allow_private),
        saved_login=s.login is not None,
        api_calls_seen=len(s.recorder.captured),
        direct_calls=s.replayer.requests_made,
        third_party_hosts=third_party,
        blocked=sorted(s._blocked),
        llm_calls=s.gateway.usage.answered if s.gateway is not None else 0,
    )


def _saved_login(store: Store, site: str, bus: EventBus) -> dict[str, Any] | None:
    """The site's saved login (ghost-api login), announced in the run log."""
    try:
        found = auth_state.load(store.path, site)
    except auth_state.LoginError as exc:
        bus.emit(Notice(str(exc), "warning"))
        return None
    if found is None:
        return None
    state, meta = found
    days = auth_state.age_days(meta)
    note = f"Using your saved login for {site} (saved {days:.0f} day(s) ago)"
    bus.emit(Notice(note, "warning" if days > 14 else "info",
                    "If pages show you logged out, run ghost-api login again." if days > 14
                    else ""))  # fmt: skip
    return state


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
            challenge = s.visit(AUTO_SCROLLS)
            if challenge is not None:
                return "bot-protection", f"bot-protection page detected: {challenge!r}"
            if s.stop is not None and s.stop():
                return "interrupted", "stopped by user"
            return "captured", "--no-agent: scrolled the page"
        result = s.explore(s.goal, opts.max_steps, opts.max_minutes, ask_user=ask_user)
        return result.status, result.reason
    except KeyboardInterrupt:
        return "interrupted", "stopped by user"
    except Exception as exc:
        s.emit(Notice(friendly_error(exc), "error", f"error code: {error_code(exc)}"))
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
        s.emit(Notice(friendly_error(exc), "error", f"error code: {error_code(exc)}"))
        return status, friendly_error(exc)
    if missing:
        names = ", ".join(e.label for e in missing)
        if status == "refreshed" and s.gateway is not None:
            # Looked for with every tool we have and still not seen: the site dropped them.
            # They stay in the history but leave the client, report and runtime.
            s.store.mark_gone(mem.domain, [e.key for e in missing], s.run_id)
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
    store, site = s.store, s.site
    reports: dict[str, ProbeReport] = {}
    if not opts.no_probe and status not in STOPPED:
        s.phase("test")
        reports = probe_all(s)

    endpoints = analyze(s.recorder.captured)
    store.save_endpoints(site, s.run_id, endpoints)
    for ep in endpoints:
        report = reports.get(ep.key)
        if report is not None:
            apply_probe(ep, report)
            store.save_probe_report(site, s.run_id, ep, report)
    s.emit(EndpointsUpdated([endpoint_summary(e, site) for e in endpoints]))
    exposed = exposures(endpoints, s.recorder.captured)
    if exposed:
        s.emit(
            Notice(
                f"Personal data: {summary_line(exposed)}. Ghost API hid these values.",
                "warning",
                "\n".join(f"{e.label}: {', '.join(e.kinds)}" for e in exposed),
            )
        )

    generated: GenerationResult | None = None
    generation_error = ""
    if not opts.no_generate and endpoints and status not in ("interrupted", "bot-protection"):
        s.phase("generate")
        plan = plan_for(store, s.gateway, site, endpoints)
        try:
            if opts.no_verify:
                generated = generate_from(
                    site, s.start_url, endpoints, s.recorder.captured, opts.out, plan, opts.lang,
                    store=store,
                )  # fmt: skip
            else:
                s.phase("verify")
                s.emit(Notice("Verifying the generated client against the live API..."))
                verifier = Verifier(
                    site,
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
                    store=store,
                )
                verification = verifier.run()
                generated = verification.result
                endpoints = verifier.endpoints
                store.save_endpoints(site, s.run_id, endpoints)  # e.g. relaxed schemas
                for ep in endpoints:
                    state, note = verification.statuses.get(ep.key, ("unverified", ""))
                    ep.status, ep.status_note = state, note
                    store.set_endpoint_status(site, ep, state, note)
                s.emit(EndpointsUpdated([endpoint_summary(e, site) for e in endpoints]))
        except (NothingToGenerate, GenerationError) as exc:
            generation_error = str(exc)
            s.emit(Notice(f"No client generated: {exc}", "warning"))
    s.replayer.close()
    if generated is not None:
        version = drift.record(store, site, generated.spec, s.run_id)
        generated.version = version
        breaking = any(c.severity == "breaking" for c in version.changes)
        level: Literal["info", "warning"] = "warning" if breaking else "info"
        s.emit(
            Notice(
                f"Client version: {version.summary()}",
                level,
                "\n".join(c.line() for c in version.changes[:12]) if version.changes else "",
            )
        )
        s.emit(client_event(generated))

    # Workflow stage 8: remember what was learned.
    s.phase("learn")
    auto = discovery_lessons([(r.action, r.args, r.new_endpoints) for r in s.steps])
    learned = store.add_lessons(site, s.run_id, auto, "auto")
    if s.gateway is not None and len(s.steps) >= MIN_STEPS_FOR_LESSONS:
        texts = write_lessons(
            s.gateway, s.goal, status, [(r.action, r.args, r.result) for r in s.steps], endpoints
        )
        learned += store.add_lessons(site, s.run_id, texts, "llm")
    store.finish_run(s.run_id, status)
    if s.gateway is not None:
        store.record_usage(s.run_id, s.gateway.usage.answered, s.gateway.usage.tokens)
        s.emit(usage_event(s.gateway))
        s.gateway.close()
    s.emit(network_summary(s))
    exports = write_exports(store, site, s.run_id, opts.out, generated.spec if generated else None)

    s.emit(
        RunFinished(
            status=status,
            reason=reason,
            seconds=round(time.monotonic() - started, 1),
            requests=len(s.recorder.captured),
            endpoints=len(endpoints),
            report=str(exports.report),
            openapi=str(exports.openapi),
            report_html=str(exports.html or ""),
            lessons_learned=learned,
            filtered=dict(s.recorder.dropped),
            generation_error=generation_error,
            exposure=summary_line(exposed) if exposed else "",
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
