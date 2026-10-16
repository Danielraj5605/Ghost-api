"""Explore a site one step at a time, with the decisions made elsewhere (an AI assistant).

`ghost-api mcp --explore` hands these steps to Claude (or any MCP client): the assistant
reads the page and decides; Ghost API acts with all its rules (robots.txt, rate limit,
same site only, no destructive or login elements, no private network), records every
request, and at the end tests the endpoints, writes the client, report and version, just
like `ghost-api run`. No LLM key is needed: the assistant is the agent.

Playwright's browser belongs to the thread that opened it, so each session runs all its
work on one worker thread of its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from ghost_api.agent.orchestrator import BotProtection, Explorer
from ghost_api.agent.prompts import format_page
from ghost_api.agent.tools import Decision
from ghost_api.analysis.analyzer import analyze
from ghost_api.browser.driver import BrowserSession
from ghost_api.config import check_language, default_db_path, load_env
from ghost_api.events import EventBus
from ghost_api.export.site_data import endpoint_summary
from ghost_api.memory.knowledge import load_memory, memory_prompt
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter
from ghost_api.pipeline import RunOptions, Session, finish, open_session, robots_policy
from ghost_api.workflows import NotAllowed, RunCollector

T = TypeVar("T")
MAX_STEPS = 60  # a ceiling, so an assistant can't keep a browser busy forever


class DriveError(Exception):
    """Something the assistant should be told plainly (a closed session, a bad action)."""


class DriveSession:
    def __init__(
        self,
        url: str,
        goal: str,
        *,
        db: Path | None = None,
        out: Path = Path("output"),
        lang: str = "python",
        rate_limit: float = 1.0,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.url, self.goal = url, goal
        self.db = db or default_db_path()
        self.out = out
        self.lang = check_language(lang)
        self.rate_limit = rate_limit
        self.max_steps = max_steps
        self.collector = RunCollector()
        self.closed = False
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ghost-drive")
        self._store: Store | None = None
        self._session: Session | None = None
        self._browser: BrowserSession | None = None
        self._explorer: Explorer | None = None
        self._started = 0.0

    # --- public, thread-safe: every call runs on the session's own worker thread ---

    def start(self) -> dict[str, Any]:
        return self._run(self._start)

    def page(self) -> str:
        return self._run(self._page)

    def act(self, action: str, **arguments: Any) -> dict[str, Any]:
        return self._run(lambda: self._act(action, arguments))

    def endpoints(self) -> list[dict[str, Any]]:
        return self._run(self._endpoints)

    def finish(self, goal_covered: bool = False, note: str = "") -> dict[str, Any]:
        try:
            return self._run(lambda: self._finish(goal_covered, note))
        finally:
            self._worker.shutdown(wait=False)

    # --- the work ---

    def _run(self, work: Callable[[], T]) -> T:
        if self.closed:
            raise DriveError("this exploration has finished; start a new one")
        return self._worker.submit(work).result()

    def _start(self) -> dict[str, Any]:
        load_env()
        host = urlsplit(self.url).hostname or ""
        bus = EventBus()
        bus.subscribe(self.collector)
        limiter = RateLimiter(self.rate_limit)
        robots, allowed = robots_policy(self.url, False, limiter, bus)
        if not allowed:
            raise NotAllowed(f"robots.txt does not allow {self.url}")
        self._store = Store(self.db)
        self._session = s = open_session(
            self._store, bus, host, self.url, self.goal, False, limiter, robots, None, 60,
            screens_root=self.db, lang=self.lang,
        )  # fmt: skip
        known = load_memory(self._store, s.site)
        if known is not None:
            s.memory = memory_prompt(known.endpoints, known.lessons)
        self._browser = s.browser().__enter__()
        self._explorer = Explorer(
            self._browser, None, lambda: s.recorder.captured, self.goal, self.url, host,
            robots=s.robots, on_step=s.on_step, replayer=s.replayer,
            raw_requests=lambda: s.recorder.raw, add_sample=s.recorder.add, memory=s.memory,
        )  # fmt: skip
        self._started = time.monotonic()
        self._explorer.begin()
        return {
            "site": s.site,
            "memory": s.memory or "nothing known about this site yet",
            "page": self._page(),
            "endpoints": self._endpoints(),
        }

    def _require(self) -> tuple[Session, BrowserSession, Explorer]:
        if self._session is None or self._browser is None or self._explorer is None:
            raise DriveError("start the exploration first (open_site)")
        return self._session, self._browser, self._explorer

    def _page(self) -> str:
        session, browser, _ = self._require()
        return format_page(browser.snapshot(session.host, session.robots))

    def _act(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        _, _, explorer = self._require()
        if action in ("finish", "ask_user"):
            raise DriveError(f"'{action}' is not a step here: use finish_exploration instead")
        if len(explorer.steps) >= self.max_steps:
            raise DriveError(f"step limit reached ({self.max_steps}); finish the exploration")
        try:
            decision = Decision(thought=str(arguments.pop("why", "") or "(assistant)"),
                                action=action, **arguments)  # type: ignore[arg-type]  # fmt: skip
        except ValueError as exc:
            raise DriveError(f"not a valid {action}: {exc}") from exc
        try:
            record = explorer.act(decision)
        except BotProtection as exc:
            raise DriveError(
                f"bot-protection page ({exc}): Ghost API never tries to get past it. Finish, "
                "or record the site yourself and use `ghost-api import`."
            ) from exc
        return {
            "result": record.result,
            "new_endpoints": record.new_endpoints,
            "page": self._page(),
        }

    def _endpoints(self) -> list[dict[str, Any]]:
        session, _, _ = self._require()
        found = analyze(session.recorder.captured)
        return [_compact(endpoint_summary(e, session.site)) for e in found]

    def _finish(self, goal_covered: bool, note: str) -> dict[str, Any]:
        session, browser, explorer = self._require()
        self.closed = True
        try:
            browser.__exit__(None, None, None)
            covered = goal_covered and bool(analyze(session.recorder.captured))
            status = "goal-covered" if covered else "finished"
            reason = note or f"explored by an assistant in {len(explorer.steps)} step(s)"
            opts = RunOptions(lang=self.lang, max_steps=self.max_steps, out=self.out)
            finish(session, status, reason, self._started, opts)
        finally:
            if self._store is not None:
                self._store.close()
        data = self.collector.data
        client = data.get("client") or {}
        return {
            "status": data.get("status"),
            "site": data.get("site"),
            "endpoints": [_compact(e) for e in data.get("endpoints", [])],
            "client": {
                "class": client.get("class_name"),
                "directory": client.get("directory"),
                "files": client.get("files", []),
            }
            if client
            else None,
            "report": data.get("report_html") or data.get("report"),
            "openapi": data.get("openapi"),
            "network": data.get("network"),
        }


def _compact(summary: dict[str, Any]) -> dict[str, Any]:
    """What an assistant needs to decide, in few tokens: no schemas or examples."""
    keep = ("method", "host", "path", "status", "statuses", "samples", "pagination",
            "auth_type", "fields", "confidence")  # fmt: skip
    out = {k: summary[k] for k in keep if summary.get(k) not in (None, [], {}, "")}
    params = summary.get("params") or []
    if params:
        out["params"] = [f"{p['name']} ({p['location']})" for p in params][:12]
    return out
