"""SQLite storage for runs and captured traffic. Later stages add more tables here."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ghost_api.capture.models import CapturedRequest

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT NOT NULL,
    start_url   TEXT NOT NULL,
    goal        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    llm_calls   INTEGER NOT NULL DEFAULT 0,
    tokens      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    method            TEXT NOT NULL,
    url               TEXT NOT NULL,
    status            INTEGER NOT NULL,
    resource_type     TEXT NOT NULL,
    req_headers       TEXT NOT NULL,
    req_body          TEXT,
    req_content_type  TEXT,
    resp_content_type TEXT,
    resp_body         TEXT,
    truncated         INTEGER NOT NULL DEFAULT 0,
    timestamp         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requests_run ON requests(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_domain ON runs(domain);
"""


@dataclass(frozen=True)
class RunRow:
    id: int
    domain: str
    start_url: str
    goal: str
    started_at: str
    finished_at: str | None
    status: str
    request_count: int


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str) -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- runs ---

    def create_run(self, domain: str, start_url: str, goal: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (domain, start_url, goal, started_at) VALUES (?, ?, ?, ?)",
            (domain, start_url, goal, _now()),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?", (status, _now(), run_id)
        )
        self.conn.commit()

    def list_runs(self, domain: str | None = None) -> list[RunRow]:
        sql = """
            SELECT r.*, (SELECT COUNT(*) FROM requests q WHERE q.run_id = r.id) AS request_count
            FROM runs r {where} ORDER BY r.id DESC
        """
        if domain:
            rows = self.conn.execute(sql.format(where="WHERE r.domain = ?"), (domain,))
        else:
            rows = self.conn.execute(sql.format(where=""))
        return [
            RunRow(
                id=r["id"],
                domain=r["domain"],
                start_url=r["start_url"],
                goal=r["goal"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                status=r["status"],
                request_count=r["request_count"],
            )
            for r in rows
        ]

    # --- requests ---

    def add_request(self, run_id: int, req: CapturedRequest) -> int:
        cur = self.conn.execute(
            """INSERT INTO requests (run_id, method, url, status, resource_type, req_headers,
                   req_body, req_content_type, resp_content_type, resp_body, truncated, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                req.method,
                req.url,
                req.status,
                req.resource_type,
                json.dumps(req.req_headers),
                req.req_body,
                req.req_content_type,
                req.resp_content_type,
                req.resp_body,
                int(req.truncated),
                req.timestamp,
            ),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def requests_for_run(self, run_id: int) -> list[CapturedRequest]:
        rows = self.conn.execute("SELECT * FROM requests WHERE run_id = ? ORDER BY id", (run_id,))
        return [
            CapturedRequest(
                method=r["method"],
                url=r["url"],
                status=r["status"],
                resource_type=r["resource_type"],
                req_headers=json.loads(r["req_headers"]),
                req_body=r["req_body"],
                req_content_type=r["req_content_type"],
                resp_content_type=r["resp_content_type"],
                resp_body=r["resp_body"],
                truncated=bool(r["truncated"]),
                timestamp=r["timestamp"],
            )
            for r in rows
        ]
