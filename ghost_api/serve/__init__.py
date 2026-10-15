"""Use sites from other tools: a local REST API (`rest.py`) and an MCP server (`mcp.py`).

Both serve the operations of `SiteRuntime`: read-only, rate-limited, robots.txt respected,
personal data hidden. One runtime per site, used one call at a time, so a busy client
can't make Ghost API hammer a site.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ghost_api.memory.store import Store
from ghost_api.runtime import Operation, OperationError, SiteRuntime

__all__ = [
    "MAX_PAGES",
    "ServedSite",
    "coerce",
    "load_sites",
    "tool_name",
]

MAX_PAGES = 20
_COMMON_LABELS = {"www", "com", "in", "org", "net", "io", "co"}


@dataclass
class ServedSite:
    site: str
    runtime: SiteRuntime
    operations: list[Operation]
    lock: threading.Lock = field(default_factory=threading.Lock)

    def fetch(
        self, op: Operation, params: dict[str, str], pages: int, max_rows: int
    ) -> dict[str, Any]:
        """All rows for these inputs (one call at a time per site)."""
        records: list[Any] = []
        hidden = 0
        read = 0
        error = ""
        with self.lock:
            for page in self.runtime.iterate(op, params, min(pages, MAX_PAGES), max_rows):
                read += 1
                if not page.result.ok:
                    error = page.result.error or f"HTTP {page.result.status}"
                    break
                records += page.records
                hidden += sum(int(h.get("count", 1)) for h in page.result.hidden)
        return {
            "site": self.site,
            "operation": op.name,
            "rows": len(records),
            "pages": read,
            "personal_values_hidden": hidden,
            "error": error,
            "data": records,
        }


def load_sites(db: Path, names: list[str] | None = None) -> dict[str, ServedSite]:
    """The sites to serve: the ones named, or every stored site with something callable."""
    with Store(db) as store:
        sites = names or sorted({r.domain for r in store.list_runs()})
        served = {}
        for site in sites:
            try:
                rt = SiteRuntime(store, site)
            except OperationError:
                if names:
                    raise
                continue
            ops = rt.operations()
            if ops:
                served[site] = ServedSite(site, rt, ops)
            else:
                rt.close()
    return served


def tool_name(site: str, op: Operation) -> str:
    """'turftown.in/chennai' + get_nearby_venues -> 'turftown_chennai_get_nearby_venues'."""
    host, _, section = site.partition("/")
    labels = [p for p in host.lower().split(".") if p not in _COMMON_LABELS]
    slug = "_".join([*labels[:2], section] if section else labels[:2]) or "site"
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{slug}_{op.name}")[:64]


def coerce(value: Any) -> str:
    """Inputs arrive as JSON values (MCP) or text (REST); runtime takes text."""
    if isinstance(value, list | dict):
        import json

        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
