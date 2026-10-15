"""Structured events emitted while a run happens.

The pipeline never prints. It emits these events, and each interface renders them:
the terminal (`ghost_api/ui/terminal.py`), the web dashboard (`ghost_api/web/`), and the
database (`run_events`), so any past run can be replayed in full.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, Literal

Phase = Literal["checks", "explore", "test", "generate", "verify", "learn", "done"]
PHASES: tuple[Phase, ...] = ("checks", "explore", "test", "generate", "verify", "learn", "done")
Level = Literal["info", "success", "warning", "error"]


_log = logging.getLogger("ghost_api")


@dataclass
class Event:
    kind: ClassVar[str] = "event"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.kind, **asdict(self)}


@dataclass
class RunStarted(Event):
    kind: ClassVar[str] = "run_started"
    run_id: int
    domain: str
    start_url: str
    goal: str
    mode: Literal["run", "refresh"] = "run"
    lang: str = "python"


@dataclass
class PhaseChanged(Event):
    kind: ClassVar[str] = "phase"
    phase: Phase


@dataclass
class Notice(Event):
    """A message for the person: progress, warnings, errors (already plain-language)."""

    kind: ClassVar[str] = "notice"
    text: str
    level: Level = "info"
    detail: str = ""


@dataclass
class RequestCaptured(Event):
    kind: ClassVar[str] = "request"
    method: str
    url: str  # redacted
    status: int
    third_party: bool = False


@dataclass
class AgentStep(Event):
    kind: ClassVar[str] = "step"
    step: int
    action: str
    args: dict[str, Any]
    thought: str
    result: str
    new_endpoints: list[str] = field(default_factory=list)
    requests: int = 0
    screenshot: str | None = None  # file name in the run's screenshot folder
    box: dict[str, float] | None = None  # element acted on: x, y, width, height (page px)


@dataclass
class Screenshot(Event):
    """A browser screenshot outside agent steps (e.g. the first page load)."""

    kind: ClassVar[str] = "screenshot"
    name: str
    url: str


@dataclass
class EndpointsUpdated(Event):
    """The current endpoint map (summaries; full details come from the API/database)."""

    kind: ClassVar[str] = "endpoints"
    endpoints: list[dict[str, Any]]


@dataclass
class HypothesisTested(Event):
    kind: ClassVar[str] = "hypothesis"
    endpoint: str  # display label
    text: str
    result: Literal["confirmed", "rejected", "inconclusive"]
    evidence: str


@dataclass
class ProbeFinished(Event):
    kind: ClassVar[str] = "probe_done"
    endpoints: int
    direct_calls: int


@dataclass
class VerifyRound(Event):
    kind: ClassVar[str] = "verify_round"
    number: int
    passed: int
    failed: int
    skipped: int
    failures: list[dict[str, str]] = field(default_factory=list)  # name, message
    repairs: list[dict[str, str]] = field(default_factory=list)  # method, kind, action


@dataclass
class ClientGenerated(Event):
    kind: ClassVar[str] = "client"
    class_name: str
    lang: str
    named_by_llm: bool
    directory: str
    methods: list[dict[str, Any]]  # name, args, returns, iterator
    credentials: list[dict[str, str]] = field(default_factory=list)  # arg, env
    skipped: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


@dataclass
class UsageUpdated(Event):
    kind: ClassVar[str] = "usage"
    answered: int
    tokens: int
    cache_hits: int = 0
    retries: int = 0
    fallbacks: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


@dataclass
class RunFinished(Event):
    kind: ClassVar[str] = "run_finished"
    status: str
    reason: str
    seconds: float
    requests: int
    endpoints: int
    report: str = ""
    openapi: str = ""
    report_html: str = ""
    lessons_learned: int = 0
    filtered: dict[str, int] = field(default_factory=dict)
    generation_error: str = ""
    exposure: str = ""  # "1 endpoint exposes personal data without a login: ..."


Subscriber = Callable[[Event], object]  # return values are ignored


class EventBus:
    """Delivers events to subscribers, in order. Thread-safe; a failing subscriber never
    breaks the run (its error is kept in `errors`)."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        self.errors: list[str] = []
        self.started = time.monotonic()

    def subscribe(self, callback: Subscriber) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def emit(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001 - interfaces must not stop the run
                message = f"{type(exc).__name__}: {exc}"
                if not self.errors:  # say it once, so a broken display can't flood the log
                    _log.warning("A display or listener failed (the run continues): %s", message)
                self.errors.append(message)
