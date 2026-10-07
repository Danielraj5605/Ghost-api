"""Watch & alerts: re-check an endpoint on a schedule and report what's new, changed or gone.

A watch is (site, operation, inputs). Each check fetches the pages through `SiteRuntime`,
keys every record by its ID-like field (or a content hash), compares it with the last
snapshot, stores the changes, and sends notifications. The first check only records a
baseline. Personal data stays hidden, as everywhere else.

Notification targets:
  ntfy:TOPIC    push to your phone with the free ntfy app (https://ntfy.sh). Topics are
                public by name, so pick a long, unguessable one.
  webhook:URL   POST the changes as JSON (Discord, Slack, Zapier, your own server...).
  desktop       a desktop notification (needs `pip install plyer`).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ghost_api.analysis.detectors import item_fingerprints
from ghost_api.memory.store import Store
from ghost_api.runtime import OperationError, SiteRuntime, flatten

# Fields that change on every visit and would make everything look "changed".
_VOLATILE_RE = re.compile(
    r"(^|[._])(updated|modified|fetched|crawled|last_seen|timestamp|time|ts|now|distance|"
    r"rank|ranking|position|score|views?|view_count|seconds_ago|ago)($|[._])|"
    r"(_at|At|_time|Time)$|_ranking|highlight",
    re.I,
)
_TITLE_FIELDS = ("name", "title", "label", "headline", "venue.name", "text", "slug", "id")
MAX_CHANGES_LISTED = 8


@dataclass
class Change:
    kind: str  # "new", "changed" or "gone"
    key: str
    title: str
    fields: dict[str, list[Any]] = field(default_factory=dict)  # name -> [old, new]

    def line(self) -> str:
        if self.kind == "new":
            return f"+ {self.title}"
        if self.kind == "gone":
            return f"- {self.title}"
        diffs = ", ".join(f"{k}: {_short(o)} -> {_short(n)}" for k, (o, n) in self.fields.items())
        return f"~ {self.title}: {diffs}"


@dataclass
class CheckResult:
    watch: dict[str, Any]
    rows: int = 0
    baseline: bool = False
    complete: bool = True  # every page was read, so "gone" can be trusted
    changes: list[Change] = field(default_factory=list)
    error: str = ""
    notified: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out = {"new": 0, "changed": 0, "gone": 0}
        for c in self.changes:
            out[c.kind] += 1
        return out

    def headline(self) -> str:
        w = self.watch
        where = f"{w['site']} · {w['operation']}"
        if self.error:
            return f"{where}: check failed ({self.error})"
        if self.baseline:
            return f"{where}: watching {self.rows} items (first check, nothing to compare yet)"
        if not self.changes:
            return f"{where}: no changes in {self.rows} items"
        parts = [f"{n} {k}" for k, n in self.counts.items() if n]
        return f"{where}: {', '.join(parts)}"

    def text(self) -> str:
        lines = [self.headline()]
        lines += [c.line() for c in self.changes[:MAX_CHANGES_LISTED]]
        if len(self.changes) > MAX_CHANGES_LISTED:
            lines.append(f"... and {len(self.changes) - MAX_CHANGES_LISTED} more")
        return "\n".join(lines)


# --- schedule -------------------------------------------------------------------------------


def parse_every(text: str) -> int:
    """'30m' -> 30, '6h' -> 360, '1d' -> 1440, '90' -> 90 (minutes)."""
    match = re.fullmatch(r"\s*(\d+)\s*([mhd]?)\s*", text.lower())
    if not match:
        raise ValueError(f"Not a duration: {text!r} (use e.g. 30m, 6h, 1d)")
    minutes = int(match.group(1)) * {"": 1, "m": 1, "h": 60, "d": 1440}[match.group(2)]
    if minutes < 5:
        raise ValueError("Check at most every 5 minutes, to be gentle with the site")
    return minutes


def is_due(watch: dict[str, Any], now: datetime | None = None) -> bool:
    if not watch.get("last_check_at"):
        return True
    last = datetime.fromisoformat(watch["last_check_at"])
    return (now or datetime.now(UTC)) >= last + timedelta(minutes=watch["every_minutes"])


def every_label(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


# --- one check ------------------------------------------------------------------------------


def check(
    store: Store,
    watch: dict[str, Any],
    runtime: SiteRuntime | None = None,
    notify: bool = True,
    sender: Callable[[str, CheckResult], str] | None = None,
) -> CheckResult:
    """Fetch, compare with the last snapshot, store, notify. Never raises for site errors."""
    result = CheckResult(watch)
    owned = runtime is None
    try:
        rt = runtime or SiteRuntime(store, watch["site"])
        try:
            op = rt.operation(watch["operation"])
            records: list[Any] = []
            pages = 0
            for page in rt.iterate(op, watch["params"], watch["max_pages"], 5000):
                pages += 1
                if not page.result.ok:
                    if not records:
                        result.error = page.result.error or f"HTTP {page.result.status}"
                    result.complete = False
                    break
                records += page.records
            if pages >= watch["max_pages"]:
                result.complete = False  # there may be more pages we didn't read
        finally:
            if owned:
                rt.close()
    except OperationError as exc:
        result.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - a watch must keep going on network errors
        result.error = f"{type(exc).__name__}: {exc}"
    if result.error:
        store.mark_watch_checked(watch["id"], f"error: {result.error}")
        return result

    current = {
        key: flatten(record)
        for key, record in zip(item_fingerprints(records), records, strict=False)
    }
    result.rows = len(current)
    previous = store.watch_items(watch["id"])
    result.baseline = not previous
    track = set(watch.get("track") or [])
    if not result.baseline:
        result.changes = diff(previous, current, track, result.complete)
    gone = [c.key for c in result.changes if c.kind == "gone"]
    status = "baseline" if result.baseline else f"{len(result.changes)} changes"
    store.save_watch_check(watch["id"], current, gone, [asdict(c) for c in result.changes], status)
    if notify and result.changes:
        for target in watch.get("notify") or []:
            outcome = (sender or send)(target, result)
            result.notified.append(outcome)
    return result


def diff(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    track: set[str],
    complete: bool,
) -> list[Change]:
    changes = []
    for key, row in current.items():
        old = previous.get(key)
        if old is None or old["gone"]:
            changes.append(Change("new", key, title_of(row)))
            continue
        fields = {
            name: [old["data"].get(name), row.get(name)]
            for name in sorted(set(row) | set(old["data"]))
            if _tracked(name, track) and _differs(old["data"].get(name), row.get(name))
        }
        if fields:
            changes.append(Change("changed", key, title_of(row), fields))
    if complete:  # only then is "not seen" the same as "gone"
        for key, old in previous.items():
            if key not in current and not old["gone"]:
                changes.append(Change("gone", key, title_of(old["data"])))
    order = {"new": 0, "changed": 1, "gone": 2}
    return sorted(changes, key=lambda c: (order[c.kind], c.title))


def title_of(row: dict[str, Any]) -> str:
    for name in _TITLE_FIELDS:
        value = row.get(name)
        if value not in (None, ""):
            return _short(value, 60)
    return _short(next(iter(row.values()), "item"), 60)


def _tracked(name: str, track: set[str]) -> bool:
    if track:
        return name in track or any(name.endswith("." + t) for t in track)
    return not _VOLATILE_RE.search(name) and not name.startswith("_")


def _differs(old: Any, new: Any) -> bool:
    return str(old) != str(new)


def _short(value: Any, limit: int = 40) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- notifications --------------------------------------------------------------------------


def send(target: str, result: CheckResult) -> str:
    """Deliver one notification; returns what happened (never raises)."""
    kind, _, where = target.partition(":")
    try:
        if kind == "ntfy" and where:
            base = os.getenv("GHOST_API_NTFY_URL", "https://ntfy.sh").rstrip("/")
            httpx.post(
                f"{base}/{where}",
                content=result.text().encode("utf-8"),
                headers={"Title": "Ghost API: changes found", "Tags": "ghost"},
                timeout=15,
            ).raise_for_status()
        elif kind == "webhook" and where:
            payload = {
                "text": result.text(),  # Slack / generic
                "content": result.text()[:1900],  # Discord
                "site": result.watch["site"],
                "operation": result.watch["operation"],
                "counts": result.counts,
                "changes": [asdict(c) for c in result.changes],
            }
            httpx.post(where, json=payload, timeout=15).raise_for_status()
        elif kind == "desktop":
            return _desktop(result)
        else:
            return f"{target}: unknown target (use ntfy:TOPIC, webhook:URL or desktop)"
    except httpx.HTTPError as exc:
        return f"{kind}: failed ({type(exc).__name__})"
    return f"{kind}: sent"


def _desktop(result: CheckResult) -> str:
    try:
        from plyer import (
            notification,  # type: ignore[import-not-found,import-untyped,unused-ignore]
        )
    except ImportError:
        return "desktop: install it with `pip install plyer`"
    try:
        notification.notify(title="Ghost API", message=result.text()[:250], timeout=10)
    except Exception as exc:  # noqa: BLE001 - notification backends vary by system
        return f"desktop: failed ({type(exc).__name__})"
    return "desktop: sent"


# --- running many ---------------------------------------------------------------------------


def run_due(store: Store, only_due: bool = True) -> list[CheckResult]:
    return [check(store, w) for w in store.watches() if not only_due or is_due(w)]


def loop(store: Store, on_result: Callable[[CheckResult], None], tick_s: float = 60) -> None:
    """Check due watches forever (until Ctrl+C)."""
    while True:
        for result in run_due(store):
            on_result(result)
        time.sleep(tick_s)


def describe(watch: dict[str, Any]) -> str:
    inputs = " ".join(f"{k}={v}" for k, v in watch["params"].items())
    return f"{watch['site']} {watch['operation']} {inputs}".strip()


def to_json(result: CheckResult) -> dict[str, Any]:
    return {
        "watch_id": result.watch["id"],
        "headline": result.headline(),
        "rows": result.rows,
        "baseline": result.baseline,
        "complete": result.complete,
        "error": result.error,
        "counts": result.counts,
        "changes": [asdict(c) for c in result.changes],
        "notified": result.notified,
    }
