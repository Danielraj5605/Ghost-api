"""The run and refresh workflows, shared by the command line and the Python API.

Everything a caller sees comes through the EventBus (a terminal renderer, the dashboard,
or `RunCollector` for a plain result). Problems a caller must handle are exceptions:
`NotAllowed` (robots.txt), `NoLLMKey`, `NothingRemembered`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ghost_api.config import Settings, state_dir
from ghost_api.events import (
    ClientGenerated,
    EndpointsUpdated,
    EventBus,
    NetworkSummary,
    Notice,
    RunFinished,
    RunStarted,
)
from ghost_api.llm.gateway import LLMGateway
from ghost_api.llm.providers import default_providers
from ghost_api.memory.knowledge import load_memory, memory_prompt
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter
from ghost_api.pipeline import (
    RunOptions,
    explore_site,
    finish,
    open_session,
    refresh_site,
    robots_policy,
)
from ghost_api.site import site_host


class NotAllowed(Exception):
    """robots.txt (or the site) does not allow the start page."""


class NoLLMKey(Exception):
    """The agent was asked for, but no LLM provider has a key."""


class NothingRemembered(Exception):
    """A refresh for a site that was never explored."""


class RunCollector:
    """Collects a run's outcome from its events: subscribe it to the bus."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"status": "failed", "endpoints": [], "client": None}

    def __call__(self, event: Any) -> None:
        if isinstance(event, RunStarted):
            self.data.update(run_id=event.run_id, site=event.domain, url=event.start_url)
        elif isinstance(event, EndpointsUpdated):
            self.data["endpoints"] = event.endpoints
        elif isinstance(event, ClientGenerated):
            self.data["client"] = event.to_dict()
        elif isinstance(event, NetworkSummary):
            network = event.to_dict()
            network.pop("type", None)
            self.data["network"] = network
        elif isinstance(event, RunFinished):
            finished = event.to_dict()
            finished.pop("type", None)
            finished["endpoint_count"] = finished.pop("endpoints")  # the list stays in "endpoints"
            self.data.update(finished)


def make_gateway(max_calls: int, required: bool) -> LLMGateway | None:
    gateway = LLMGateway(
        default_providers(), max_calls=max_calls, cooldown_file=state_dir() / "cooldowns.json"
    )
    if gateway.providers:
        return gateway
    if required:
        raise NoLLMKey(
            "No LLM API key found. Add one to .env (or run `ghost-api init`), or explore "
            "without the agent (the page is only loaded and scrolled)."
        )
    return None


def run(
    settings: Settings,
    opts: RunOptions,
    bus: EventBus,
    *,
    use_agent: bool = True,
    ask_user: Callable[[str], str] | None = None,
    stop: Callable[[], bool] | None = None,
    started_hook: Callable[[], None] | None = None,
) -> str:
    """Explore a site, test what was found, write the client and report. Returns the run's
    status. `started_hook`: called once the checks passed (e.g. to start a live display)."""
    limiter = RateLimiter(settings.rate_limit)
    robots, allowed = robots_policy(settings.url, settings.ignore_robots, limiter, bus)
    if not allowed:
        raise NotAllowed(f"robots.txt does not allow {settings.url}")
    gateway = make_gateway(settings.max_llm_calls, required=True) if use_agent else None
    host = urlsplit(settings.url).hostname or ""
    with Store(settings.db_path) as store:
        if started_hook is not None:
            started_hook()
        s = open_session(
            store, bus, host, settings.url, settings.goal, settings.headed, limiter, robots,
            gateway, opts.max_probes, screens_root=settings.db_path, lang=settings.lang,
        )  # fmt: skip
        if stop is not None:
            s.stop = stop
        known = load_memory(store, s.site)
        if known is not None:
            s.memory = memory_prompt(known.endpoints, known.lessons)
            bus.emit(Notice(f"Using memory: {len(known.endpoints)} known endpoints, "
                            f"{len(known.lessons)} lessons"))  # fmt: skip
        started = time.monotonic()
        status, reason = explore_site(s, opts, use_agent=gateway is not None, ask_user=ask_user)
        finish(s, status, reason, started, opts)
    return status


def refresh(
    db_path: Path,
    site: str,
    opts: RunOptions,
    bus: EventBus,
    *,
    use_llm: bool = True,
    max_llm_calls: int = 20,
    rate_limit: float = 1.0,
    ignore_robots: bool = False,
    stop: Callable[[], bool] | None = None,
    started_hook: Callable[[], None] | None = None,
) -> tuple[str, int, int]:
    """Re-check a known site from memory. Returns (status, LLM calls now, on the first run)."""
    with Store(db_path) as store:
        mem = load_memory(store, site)
        if mem is None:
            raise NothingRemembered(f"Nothing remembered for {site}. Run `ghost-api run` first.")
        bus.emit(Notice(f"Refreshing {site} from memory: {len(mem.endpoints)} endpoint(s), "
                        f"{len(mem.lessons)} lesson(s)"))  # fmt: skip
        limiter = RateLimiter(rate_limit)
        robots, allowed = robots_policy(mem.start_url, ignore_robots, limiter, bus)
        if not allowed:
            raise NotAllowed(f"robots.txt does not allow {mem.start_url}")
        gateway = make_gateway(max_llm_calls, required=False) if use_llm else None
        if started_hook is not None:
            started_hook()
        s = open_session(
            store, bus, site_host(site), mem.start_url, f"refresh: {mem.goal}", opts.headed,
            limiter, robots, gateway, opts.max_probes, screens_root=db_path, mode="refresh",
            lang=opts.lang, site=site,
        )  # fmt: skip
        if stop is not None:
            s.stop = stop
        s.memory = memory_prompt(mem.endpoints, mem.lessons)
        started = time.monotonic()
        status, reason = refresh_site(s, mem, opts.max_steps)
        finish(s, status, reason, started, opts)
        now = gateway.usage.answered if gateway is not None else 0
        return status, now, store.first_run_llm_calls(site) or 0
