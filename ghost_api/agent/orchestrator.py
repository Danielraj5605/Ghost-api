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

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from ghost_api.agent.prompts import SYSTEM_PROMPT, HistoryItem, step_prompt
from ghost_api.agent.tools import Decision, is_typeable
from ghost_api.analysis.analyzer import analyze
from ghost_api.analysis.models import Endpoint
from ghost_api.browser.driver import ActionError
from ghost_api.browser.snapshot import PageSnapshot
from ghost_api.capture.filters import is_same_site
from ghost_api.capture.models import CapturedRequest
from ghost_api.llm.gateway import BudgetExceeded, LLMReply, LLMUnavailable
from ghost_api.net import RobotsPolicy

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


@dataclass
class AgentResult:
    status: str  # goal-covered | finished | max-steps | no-progress | time-limit |
    #              llm-budget | llm-unavailable | interrupted
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
        self.steps: list[StepRecord] = []
        self.history: list[HistoryItem] = []

    # --- main loop ---

    def run(self) -> AgentResult:
        started = self.clock()
        try:
            self.browser.goto(self.start_url)
            known = {e.key for e in self._endpoints()}
            since_progress = 0
            for step in range(1, self.max_steps + 1):
                if self.clock() - started > self.deadline_s:
                    return self._result("time-limit", "time limit reached")
                snapshot = self.browser.snapshot(self.host, self.robots)
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

                result, new_requests = self._execute(decision, snapshot)
                endpoints = self._endpoints()
                new = [self._label(e) for e in endpoints if e.key not in known]
                known |= {e.key for e in endpoints}
                self._record(step, decision, result, new, len(new_requests))

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

    def _record(
        self, step: int, d: Decision, result: str, new_endpoints: list[str], requests: int
    ) -> None:
        if new_endpoints:
            result += f"; NEW ENDPOINT: {', '.join(new_endpoints)}"
        rec = StepRecord(step, d.thought, d.action, d.args(), result, new_endpoints, requests)
        self.steps.append(rec)
        self.history.append(HistoryItem(step, d.action, d.args(), result))
        if self.on_step:
            self.on_step(rec)

    def _result(self, status: str, reason: str) -> AgentResult:
        return AgentResult(status, reason, self.steps, self._endpoints())


class _Rejected(Exception):
    pass


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
