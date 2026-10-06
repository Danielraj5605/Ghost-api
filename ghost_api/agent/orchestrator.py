"""The exploration loop: observe -> decide -> act -> learn (Stage 4).

Stopping rules (any one ends the loop):
  - the agent finishes (goal covered, or the site has no API for it)
  - the step budget is used up
  - the LLM call budget is used up, or no provider can answer
  - no new endpoint for NO_PROGRESS_STEPS steps in a row
  - the time limit is reached
  - the user presses Ctrl+C (partial results are kept)
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from ghost_api.agent.prompts import SYSTEM_PROMPT, HistoryItem, step_prompt
from ghost_api.agent.tools import Decision, is_typeable
from ghost_api.analysis.analyzer import analyze, endpoint_keys
from ghost_api.analysis.detectors import find_items, is_bot_challenge
from ghost_api.analysis.models import Endpoint
from ghost_api.browser.driver import ActionError
from ghost_api.browser.snapshot import PageSnapshot
from ghost_api.capture.filters import is_same_site, redact_text
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.llm.gateway import BudgetExceeded, LLMReply, LLMUnavailable
from ghost_api.net import RobotsPolicy
from ghost_api.probe.replay import (
    Replayer,
    ReplayRefused,
    is_replayable,
    with_overrides,
    with_path_params,
)

NO_PROGRESS_STEPS = 8


class Browser(Protocol):
    """What the orchestrator needs from a browser (BrowserSession, or a fake in tests)."""

    popup_urls: list[str]

    @property
    def url(self) -> str: ...

    def snapshot(self, target_host: str, robots: RobotsPolicy | None = None) -> PageSnapshot: ...
    def goto(self, url: str) -> list[CapturedRequest]: ...
    def click(self, element_id: int) -> list[CapturedRequest]: ...
    def type_text(self, element_id: int, text: str, submit: bool) -> list[CapturedRequest]: ...
    def scroll(self, direction: str = "down") -> list[CapturedRequest]: ...
    def go_back(self) -> list[CapturedRequest]: ...


class LLM(Protocol):
    def ask(
        self,
        messages: list[dict[str, str]],
        response_model: type[Decision] | None = None,
        temperature: float = 0.0,
    ) -> LLMReply[Decision]: ...


@dataclass
class StepRecord:
    step: int
    thought: str
    action: str
    args: dict[str, object]
    result: str
    new_endpoints: list[str] = field(default_factory=list)
    requests: int = 0
    screenshot: str | None = None  # name of the screenshot taken after this step
    box: dict[str, float] | None = None  # the element clicked or typed into


@dataclass
class AgentResult:
    status: str  # goal-covered | finished | max-steps | no-progress | time-limit |
    #              llm-budget | llm-unavailable | bot-protection | interrupted
    reason: str
    steps: list[StepRecord]
    endpoints: list[Endpoint]


class Explorer:
    def __init__(
        self,
        browser: Browser,
        llm: LLM,
        captured: Callable[[], list[CapturedRequest]],
        goal: str,
        start_url: str,
        target_host: str,
        max_steps: int = 40,
        max_minutes: float = 15.0,
        robots: RobotsPolicy | None = None,
        on_step: Callable[[StepRecord], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        replayer: Replayer | None = None,
        raw_requests: Callable[[], list[RawRequest | None]] | None = None,
        add_sample: Callable[[CapturedRequest, RawRequest], object] | None = None,
        ask_user: Callable[[str], str] | None = None,
        memory: str = "",
        screenshot: Callable[[str], str | None] | None = None,
    ) -> None:
        self.browser = browser
        self.llm = llm
        self.captured = captured
        self.goal = goal
        self.start_url = start_url
        self.host = target_host
        self.max_steps = max_steps
        self.deadline_s = max_minutes * 60
        self.robots = robots
        self.on_step = on_step
        self.clock = clock
        self.replayer = replayer
        self.raw_requests = raw_requests
        self.add_sample = add_sample
        self.ask_user = ask_user
        self.memory = memory  # notes from earlier runs on this site (Stage 8)
        # Saves a screenshot under the given name and returns it (for the live view).
        self.screenshot = screenshot
        self.steps: list[StepRecord] = []
        self.history: list[HistoryItem] = []

    # --- main loop ---

    def run(self) -> AgentResult:
        started = self.clock()
        try:
            self.browser.goto(self.start_url)
            self._shoot("start")
            known = {e.key for e in self._endpoints()}
            since_progress = 0
            for step in range(1, self.max_steps + 1):
                if self.clock() - started > self.deadline_s:
                    return self._result("time-limit", "time limit reached")
                snapshot = self.browser.snapshot(self.host, self.robots)
                if is_bot_challenge(snapshot.title, snapshot.text):
                    # Never try to get past bot protection (NFR-19).
                    return self._result(
                        "bot-protection", f"bot-protection page detected: {snapshot.title!r}"
                    )
                try:
                    decision = self._decide(step, snapshot)
                except BudgetExceeded as exc:
                    return self._result("llm-budget", str(exc))
                except LLMUnavailable as exc:
                    return self._result("llm-unavailable", str(exc))

                if decision.action == "finish":
                    self._record(step, decision, "finished", [], 0)
                    status = "goal-covered" if decision.goal_covered else "finished"
                    return self._result(status, decision.thought)

                box = self._box(decision)
                result, new_requests = self._execute(decision, snapshot)
                endpoints = self._endpoints()
                new = [self._label(e) for e in endpoints if e.key not in known]
                known |= {e.key for e in endpoints}
                shot = self._shoot(f"step-{step}")
                self._record(step, decision, result, new, len(new_requests), shot, box)

                since_progress = 0 if new else since_progress + 1
                if since_progress >= NO_PROGRESS_STEPS:
                    return self._result(
                        "no-progress", f"no new endpoint in {NO_PROGRESS_STEPS} steps"
                    )
            return self._result("max-steps", f"used all {self.max_steps} steps")
        except KeyboardInterrupt:
            return self._result("interrupted", "stopped by user")

    # --- decide ---

    def _decide(self, step: int, snapshot: PageSnapshot) -> Decision:
        prompt = step_prompt(
            self.goal,
            step,
            self.max_steps,
            snapshot,
            self._endpoints(),
            self.host,
            self.history,
            self.memory,
        )
        reply = self.llm.ask(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            response_model=Decision,
        )
        assert reply.parsed is not None  # the gateway validates structured replies
        return reply.parsed

    # --- act ---

    def _execute(
        self, decision: Decision, snapshot: PageSnapshot
    ) -> tuple[str, list[CapturedRequest]]:
        """Run one action. Returns a short result for the agent and the new requests."""
        if decision.action == "call_endpoint":
            return self._call_endpoint(decision)
        if decision.action == "ask_user":
            return self._ask(decision), []
        try:
            new = self._dispatch(decision, snapshot)
        except _Rejected as exc:
            return f"rejected: {exc}", []
        except ActionError as exc:
            return f"failed: {exc}", []

        notes: list[str] = []
        # A link that tried to open a new tab: follow it in this tab if it's allowed.
        while self.browser.popup_urls:
            popup = self.browser.popup_urls.pop(0)
            if self._allowed_url(popup):
                new += self._safe(partial(self.browser.goto, popup))
                notes.append("opened link in same tab")
        if not is_same_site(self.browser.url, self.host):
            new += self._safe(self.browser.go_back)
            notes.append("left the site, went back")
        return _describe(new, self.browser.url, notes), new

    def _dispatch(self, d: Decision, snapshot: PageSnapshot) -> list[CapturedRequest]:
        if d.action in ("click", "type"):
            assert d.element_id is not None
            el = snapshot.element(d.element_id)
            if el is None:
                raise _Rejected(f"[{d.element_id}] is not in the element list")
            if d.action == "click":
                return self.browser.click(d.element_id)
            if not is_typeable(el.kind):
                raise _Rejected(f"[{d.element_id}] is a {el.kind}, not a text field")
            assert d.text is not None
            return self.browser.type_text(d.element_id, d.text, d.submit)
        if d.action == "scroll":
            return self.browser.scroll(d.direction or "down")
        if d.action == "navigate":
            assert d.url is not None
            url = urljoin(self.browser.url, d.url)
            if not self._allowed_url(url):
                raise _Rejected(f"{d.url} is off-site, not http(s), or disallowed by robots.txt")
            return self.browser.goto(url)
        if d.action == "go_back":
            return self.browser.go_back()
        raise _Rejected(f"unknown action {d.action}")

    def _call_endpoint(self, d: Decision) -> tuple[str, list[CapturedRequest]]:
        if self.replayer is None or self.raw_requests is None:
            return "rejected: calling endpoints directly is not enabled", []
        endpoints = self._endpoints()
        target = _find_endpoint(d.endpoint or "", endpoints, self.host)
        if target is None:
            return f"rejected: no endpoint named {d.endpoint!r}; use a name as listed", []
        keys = endpoint_keys(self.captured())
        raws = self.raw_requests()
        samples = [r for k, r in zip(keys, raws, strict=False) if k == target.key and r]
        sample = next((s for s in reversed(samples) if is_replayable(s)), None)
        if sample is None:
            return "rejected: this endpoint can't be called safely (not a read-only request)", []
        # Each parameter goes where the endpoint expects it: path, JSON body or query.
        params = d.params or {}
        path_names = {p.name for p in target.path_params}
        body_keys = _body_keys(sample) - path_names
        path = {k: v for k, v in params.items() if k in path_names}
        body = {k: _typed(v) for k, v in params.items() if k in body_keys}
        query: dict[str, str | None] = {
            k: v for k, v in params.items() if k not in path_names and k not in body_keys
        }
        request = with_path_params(sample, target.path_template, path)
        request = with_overrides(request, query=query or None, body=body or None)
        try:
            result = self.replayer.send(request)
        except ReplayRefused as exc:
            return f"rejected: {exc}", []
        if result.captured is None:
            return f"failed: {result.error}", []
        if self.add_sample:
            self.add_sample(result.captured, request)
        items = find_items(result.body) if result.ok else None
        summary = f"HTTP {result.status}"
        if result.error:
            summary += f", {result.error}"
        elif items is not None:
            summary += f", {len(items)} records"
        return f"called {target.label} with {params}: {summary}", [result.captured]

    def _ask(self, d: Decision) -> str:
        if self.ask_user is None:
            return "rejected: no human is available (run with --headed in a terminal)"
        answer = self.ask_user(d.question or "")
        return f"user answered: {redact_text(answer)[:200] or '(no answer)'}"

    def _allowed_url(self, url: str) -> bool:
        if urlsplit(url).scheme not in ("http", "https") or not is_same_site(url, self.host):
            return False
        return self.robots is None or self.robots.allows(url)

    def _safe(self, action: Callable[[], list[CapturedRequest]]) -> list[CapturedRequest]:
        try:
            return action()
        except ActionError:
            return []

    # --- learn / record ---

    def _label(self, ep: Endpoint) -> str:
        return ep.label if ep.host == self.host else f"{ep.method} {ep.host}{ep.path_template}"

    def _endpoints(self) -> list[Endpoint]:
        return analyze(self.captured())

    def _shoot(self, name: str) -> str | None:
        if self.screenshot is None:
            return None
        try:
            return self.screenshot(name)
        except Exception:  # noqa: BLE001 - a missing screenshot must not stop the run
            return None

    def _box(self, d: Decision) -> dict[str, float] | None:
        locate = getattr(self.browser, "element_box", None)
        if d.action not in ("click", "type") or d.element_id is None or locate is None:
            return None
        try:
            box: dict[str, float] | None = locate(d.element_id)
        except Exception:  # noqa: BLE001
            return None
        return box

    def _record(
        self,
        step: int,
        d: Decision,
        result: str,
        new_endpoints: list[str],
        requests: int,
        screenshot: str | None = None,
        box: dict[str, float] | None = None,
    ) -> None:
        if new_endpoints:
            result += f"; NEW ENDPOINT: {', '.join(new_endpoints)}"
        rec = StepRecord(
            step, d.thought, d.action, d.args(), result, new_endpoints, requests, screenshot, box
        )
        self.steps.append(rec)
        self.history.append(HistoryItem(step, d.action, d.args(), result))
        if self.on_step:
            self.on_step(rec)

    def _result(self, status: str, reason: str) -> AgentResult:
        return AgentResult(status, reason, self.steps, self._endpoints())


class _Rejected(Exception):
    pass


def _find_endpoint(name: str, endpoints: list[Endpoint], host: str) -> Endpoint | None:
    """Match 'GET /api/x', 'GET host/api/x' or the prompt's '... [third-party: host]'."""
    wanted = " ".join(name.split("[", 1)[0].split()).lower()
    for ep in endpoints:
        names = {ep.label.lower(), f"{ep.method} {ep.host}{ep.path_template}".lower()}
        if wanted in names:
            return ep
    return None


def _body_keys(raw: RawRequest) -> set[str]:
    try:
        data = json.loads(raw.body or "")
    except ValueError:
        return set()
    return set(data) if isinstance(data, dict) else set()


def _typed(value: str) -> object:
    """'2' -> 2 and 'true' -> True for JSON bodies; anything else stays a string."""
    try:
        return json.loads(value)
    except ValueError:
        return value


def _describe(new: list[CapturedRequest], url: str, notes: list[str]) -> str:
    if new:
        shown = ", ".join(f"{r.method} {_path(r.url)}" for r in new[:4])
        more = f" (+{len(new) - 4} more)" if len(new) > 4 else ""
        text = f"{len(new)} new request{'s' if len(new) != 1 else ''}: {shown}{more}"
    else:
        text = "no new requests"
    text += f"; page is now {_path(url)}"
    if notes:
        text += f" ({'; '.join(notes)})"
    return text


def _path(url: str) -> str:
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return path if len(path) <= 60 else path[:57] + "..."
