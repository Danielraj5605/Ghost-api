"""Ghost API from Python: the same things the command line does, as functions.

    from ghost_api.api import discover, fetch, diff

    result = discover("https://quotes.toscrape.com/scroll", "list quotes with their authors")
    print(result.status, [e["path"] for e in result.endpoints], result.client_files)

    rows = fetch("quotes.toscrape.com/scroll", "get_quotes", pages=3)
    changes = diff("quotes.toscrape.com/scroll", live=True)

Every function respects the same rules as the CLI (robots.txt, rate limits, the network
boundary, personal data hidden) and uses the project's settings (ghost-api.toml) unless
`db` / `out` are given. Problems are exceptions: `NotAllowed`, `NoLLMKey`,
`NothingRemembered`, `GhostApiError`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ghost_api import drift, workflows
from ghost_api.config import Settings, default_db_path, load_env
from ghost_api.events import Event, EventBus
from ghost_api.generator import NothingToGenerate, generate_client, site_spec
from ghost_api.memory.store import Store
from ghost_api.pipeline import RunOptions
from ghost_api.runtime import OperationError, SiteRuntime
from ghost_api.site import SiteAmbiguous, resolve_site
from ghost_api.workflows import NoLLMKey, NotAllowed, NothingRemembered

__all__ = [
    "GhostApiError",
    "NoLLMKey",
    "NotAllowed",
    "NothingRemembered",
    "RunResult",
    "diff",
    "discover",
    "fetch",
    "generate",
    "operations",
    "refresh",
    "sites",
]


class GhostApiError(Exception):
    """Something the caller can fix: an unknown site, nothing stored yet, a bad input."""


@dataclass
class RunResult:
    status: str  # goal-covered, finished, captured, bot-protection, interrupted, failed...
    reason: str
    site: str
    run_id: int | None
    endpoints: list[dict[str, Any]]  # one summary per endpoint (path, status, params...)
    client: dict[str, Any] | None  # class name, directory, files, methods
    report: str
    report_html: str
    openapi: str
    network: dict[str, Any] = field(default_factory=dict)  # the run's permissions summary
    raw: dict[str, Any] = field(default_factory=dict)  # everything, as `--json` prints it

    @property
    def ok(self) -> bool:
        return self.status not in ("failed", "llm-unavailable", "bot-protection", "interrupted")

    @property
    def client_files(self) -> list[str]:
        if not self.client:
            return []
        return [str(Path(self.client["directory"]) / f) for f in self.client.get("files", [])]


def _result(data: dict[str, Any]) -> RunResult:
    return RunResult(
        status=str(data.get("status", "failed")),
        reason=str(data.get("reason", "")),
        site=str(data.get("site", "")),
        run_id=data.get("run_id"),
        endpoints=list(data.get("endpoints", [])),
        client=data.get("client"),
        report=str(data.get("report", "")),
        report_html=str(data.get("report_html", "")),
        openapi=str(data.get("openapi", "")),
        network=dict(data.get("network", {})),
        raw=data,
    )


def _bus(on_event: Callable[[Event], None] | None) -> tuple[EventBus, workflows.RunCollector]:
    load_env()
    bus = EventBus()
    collector = workflows.RunCollector()
    bus.subscribe(collector)
    if on_event is not None:
        bus.subscribe(on_event)
    return bus, collector


def _db(db: Path | str | None) -> Path:
    return Path(db) if db is not None else default_db_path()


def _site(store: Store, name: str) -> str:
    try:
        return resolve_site(name, {r.domain for r in store.list_runs()})
    except SiteAmbiguous as exc:
        raise GhostApiError(str(exc)) from exc


def discover(
    url: str,
    goal: str,
    *,
    use_agent: bool = True,
    lang: str = "python",
    out: Path | str = "output",
    db: Path | str | None = None,
    headed: bool = False,
    max_steps: int = 40,
    max_llm_calls: int = 50,
    max_minutes: float = 15.0,
    rate_limit: float = 1.0,
    verify: bool = True,
    probe: bool = True,
    on_event: Callable[[Event], None] | None = None,
) -> RunResult:
    """Explore a site (with the AI agent unless `use_agent=False`), test what it found,
    and write a client and report. `on_event` receives every live event."""
    settings = Settings(
        url=url, goal=goal, lang=lang, headed=headed, max_steps=max_steps,
        max_llm_calls=max_llm_calls, max_minutes=max_minutes, rate_limit=rate_limit,
        db_path=_db(db),
    )  # fmt: skip
    opts = RunOptions(lang=lang, headed=headed, max_steps=max_steps, max_minutes=max_minutes,
                      no_probe=not probe, no_verify=not verify, out=Path(out))  # fmt: skip
    bus, collector = _bus(on_event)
    workflows.run(settings, opts, bus, use_agent=use_agent)
    return _result(collector.data)


def refresh(
    site: str,
    *,
    use_llm: bool = True,
    lang: str = "python",
    out: Path | str = "output",
    db: Path | str | None = None,
    verify: bool = True,
    on_event: Callable[[Event], None] | None = None,
) -> RunResult:
    """Re-check a known site from memory: replays first, the browser only if needed."""
    path = _db(db)
    with Store(path) as store:
        name = _site(store, site)
    opts = RunOptions(lang=lang, max_steps=10, no_verify=not verify, out=Path(out))
    bus, collector = _bus(on_event)
    workflows.refresh(path, name, opts, bus, use_llm=use_llm)
    return _result(collector.data)


def generate(
    site: str, *, lang: str = "python", out: Path | str = "output", db: Path | str | None = None
) -> dict[str, Any]:
    """(Re)write a site's client from stored data (no browser, no LLM). Returns the client's
    class name, files and version."""
    with Store(_db(db)) as store:
        name = _site(store, site)
        try:
            result = generate_client(store, name, Path(out), lang=lang)
        except NothingToGenerate as exc:
            raise GhostApiError(str(exc)) from exc
    client = result.client
    return {
        "class_name": result.spec.class_name,
        "directory": str(client.directory),
        "files": [str(p) for p in (client.client_path, client.models_path, client.tests_path)],
        "version": result.version.version if result.version else result.spec.version,
    }


def operations(site: str, *, db: Path | str | None = None) -> list[dict[str, Any]]:
    """What can be fetched from a site: name, summary, endpoint, inputs (with examples)."""
    with Store(_db(db)) as store:
        name = _site(store, site)
        try:
            runtime = SiteRuntime(store, name)
        except OperationError as exc:
            raise GhostApiError(str(exc)) from exc
    with runtime:
        return [
            {"name": op.name, "summary": op.summary, "endpoint": op.label,
             "inputs": {i.name: i.example for i in op.inputs}, "pagination": op.pagination}
            for op in runtime.operations()
        ]  # fmt: skip


def fetch(
    site: str,
    operation: str,
    inputs: dict[str, str] | None = None,
    *,
    pages: int = 5,
    max_rows: int = 1000,
    db: Path | str | None = None,
) -> list[Any]:
    """Rows from one operation, following pages. Read-only, rate-limited, personal data
    hidden (like `ghost-api fetch`)."""
    with Store(_db(db)) as store:
        name = _site(store, site)
        try:
            runtime = SiteRuntime(store, name)
        except OperationError as exc:
            raise GhostApiError(str(exc)) from exc
    rows: list[Any] = []
    with runtime:
        try:
            op = runtime.operation(operation)
            for page in runtime.iterate(op, inputs or {}, pages, max_rows):
                if not page.result.ok:
                    detail = page.result.error or f"HTTP {page.result.status}"
                    if not rows:
                        raise GhostApiError(f"{operation} failed: {detail}")
                    break
                rows += page.records
        except OperationError as exc:
            raise GhostApiError(str(exc)) from exc
    return rows


def diff(site: str, *, live: bool = False, db: Path | str | None = None) -> dict[str, Any]:
    """What changed in a site's API since the last client version: version, previous and
    a list of changes ({"severity", "where", "text"})."""
    with Store(_db(db)) as store:
        name = _site(store, site)
        try:
            spec = site_spec(store, name)
        except NothingToGenerate as exc:
            raise GhostApiError(str(exc)) from exc
        info = drift.version_for(store, name, spec)
        changes = list(info.changes)
        if live:
            changes += drift.live_check(store, name)
    return {
        "site": name,
        "version": info.version,
        "previous": info.previous,
        "changes": drift.changes_json(changes),
        "breaking": sum(c.severity == "breaking" for c in changes),
    }


def sites(*, db: Path | str | None = None) -> list[str]:
    """Sites explored so far, newest first."""
    path = _db(db)
    if not path.exists():
        return []
    with Store(path) as store:
        return list(dict.fromkeys(r.domain for r in store.list_runs()))
