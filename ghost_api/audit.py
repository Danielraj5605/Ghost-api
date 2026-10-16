"""Audit log: every request Ghost API sent itself, and every request its rules blocked.

One JSON line per event, appended to `ghost_api_files/audit/<site>.jsonl` next to the
database, so a team can answer "what did this tool send to that site, and when?".
Only the method, host, path, the names of query parameters, the status and the decision
are kept: never query values, bodies, headers or cookies.

    {"at": "2026-10-17T09:12:03+00:00", "site": "example.com", "run": 12, "kind": "direct",
     "method": "GET", "host": "example.com", "path": "/api/items", "query": ["page"],
     "status": 200, "decision": "sent"}
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ghost_api.site import to_url_id

_lock = threading.Lock()


def audit_file(db_path: Path, site: str) -> Path:
    return db_path.parent / f"{db_path.stem}_files" / "audit" / f"{to_url_id(site)}.jsonl"


class AuditLog:
    def __init__(self, db_path: Path, site: str, run_id: int | None = None) -> None:
        self.path = audit_file(db_path, site)
        self.site = site
        self.run_id = run_id

    def record(self, kind: str, method: str, url: str, status: int | None, decision: str) -> None:
        parts = urlsplit(url)
        entry: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "site": self.site,
            "run": self.run_id,
            "kind": kind,  # direct (endpoint tests, fetch, watch, serve) or blocked
            "method": method.upper(),
            "host": (parts.hostname or "").lower(),
            "path": parts.path or "/",
            "query": sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)}),
            "status": status,
            "decision": decision,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def blocked(self, message: str) -> None:
        """A rule stopped a request; the message names the host and path."""
        entry = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "site": self.site,
            "run": self.run_id,
            "kind": "blocked",
            "decision": message[:300],
        }
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read(db_path: Path, site: str, limit: int | None = None) -> list[dict[str, Any]]:
    path = audit_file(db_path, site)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    return entries[-limit:] if limit else entries
