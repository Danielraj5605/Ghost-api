"""SQLite storage for runs, captured traffic and the endpoint map."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ghost_api.analysis.models import Endpoint, PathParam, QueryParam
from ghost_api.capture.models import CapturedRequest

if TYPE_CHECKING:
    from ghost_api.probe.prober import ProbeReport

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
    third_party       INTEGER NOT NULL DEFAULT 0,
    timestamp         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS endpoints (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    domain         TEXT NOT NULL,
    method         TEXT NOT NULL,
    host           TEXT NOT NULL,
    url_template   TEXT NOT NULL,
    name           TEXT,
    description    TEXT,
    auth_type      TEXT,
    pagination     TEXT,
    status         TEXT NOT NULL DEFAULT 'discovered',
    path_params    TEXT NOT NULL DEFAULT '[]',
    query_params   TEXT NOT NULL DEFAULT '[]',
    sample_count   INTEGER NOT NULL DEFAULT 0,
    statuses       TEXT NOT NULL DEFAULT '[]',
    example_urls   TEXT NOT NULL DEFAULT '[]',
    third_party    INTEGER NOT NULL DEFAULT 0,
    last_run_id    INTEGER REFERENCES runs(id),
    updated_at     TEXT NOT NULL,
    UNIQUE (domain, method, host, url_template)
);

CREATE TABLE IF NOT EXISTS schemas (
    endpoint_id  INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('request', 'response')),
    json_schema  TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    PRIMARY KEY (endpoint_id, kind)
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    step_no    INTEGER NOT NULL,
    thought    TEXT NOT NULL,
    action     TEXT NOT NULL,
    args       TEXT NOT NULL,
    result     TEXT NOT NULL,
    new_endpoints INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    endpoint_key TEXT NOT NULL,
    text         TEXT NOT NULL,
    result       TEXT NOT NULL CHECK (result IN ('confirmed', 'rejected', 'inconclusive')),
    evidence     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    prompt_hash TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requests_run ON requests(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_domain ON runs(domain);
"""


# (table, column, definition) for columns added after the first release of a table.
MIGRATIONS = (
    ("requests", "third_party", "INTEGER NOT NULL DEFAULT 0"),
    ("endpoints", "third_party", "INTEGER NOT NULL DEFAULT 0"),
    ("endpoints", "required_headers", "TEXT NOT NULL DEFAULT '[]'"),
    ("endpoints", "replayable", "INTEGER"),
)


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
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self.conn.commit()

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

    def record_usage(self, run_id: int, llm_calls: int, tokens: int) -> None:
        self.conn.execute(
            "UPDATE runs SET llm_calls = ?, tokens = ? WHERE id = ?", (llm_calls, tokens, run_id)
        )
        self.conn.commit()

    # --- requests ---

    def add_request(self, run_id: int, req: CapturedRequest) -> int:
        cur = self.conn.execute(
            """INSERT INTO requests (run_id, method, url, status, resource_type, req_headers,
                   req_body, req_content_type, resp_content_type, resp_body, truncated,
                   third_party, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                int(req.third_party),
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
                third_party=bool(r["third_party"]),
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    # --- endpoints ---

    def save_endpoints(self, domain: str, run_id: int, endpoints: list[Endpoint]) -> None:
        """Insert or update endpoints. Fields set by later stages (name, auth...) are kept."""
        for ep in endpoints:
            values = (
                json.dumps([asdict(p) for p in ep.path_params]),
                json.dumps([asdict(p) for p in ep.query_params]),
                ep.sample_count,
                json.dumps(ep.statuses),
                json.dumps(ep.example_urls),
                int(ep.third_party),
                run_id,
                _now(),
            )
            self.conn.execute(
                """INSERT INTO endpoints (path_params, query_params, sample_count, statuses,
                       example_urls, third_party, last_run_id, updated_at, domain, method, host,
                       url_template)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (domain, method, host, url_template) DO UPDATE SET
                       path_params = excluded.path_params,
                       query_params = excluded.query_params,
                       sample_count = excluded.sample_count,
                       statuses = excluded.statuses,
                       example_urls = excluded.example_urls,
                       third_party = excluded.third_party,
                       last_run_id = excluded.last_run_id,
                       updated_at = excluded.updated_at""",
                (*values, domain, ep.method, ep.host, ep.path_template),
            )
            (endpoint_id,) = self.conn.execute(
                "SELECT id FROM endpoints WHERE domain = ? AND method = ? AND host = ?"
                " AND url_template = ?",
                (domain, ep.method, ep.host, ep.path_template),
            ).fetchone()
            for kind, schema, count in (
                ("request", ep.request_schema, ep.request_samples),
                ("response", ep.response_schema, ep.response_samples),
            ):
                if schema is None:
                    continue
                self.conn.execute(
                    """INSERT INTO schemas (endpoint_id, kind, json_schema, sample_count)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT (endpoint_id, kind) DO UPDATE SET
                           json_schema = excluded.json_schema,
                           sample_count = excluded.sample_count""",
                    (endpoint_id, kind, json.dumps(schema), count),
                )
        self.conn.commit()

    def load_endpoints(self, domain: str) -> list[Endpoint]:
        rows = self.conn.execute(
            "SELECT * FROM endpoints WHERE domain = ? ORDER BY host, url_template, method",
            (domain,),
        ).fetchall()
        out: list[Endpoint] = []
        for r in rows:
            schemas = {
                s["kind"]: (json.loads(s["json_schema"]), s["sample_count"])
                for s in self.conn.execute(
                    "SELECT * FROM schemas WHERE endpoint_id = ?", (r["id"],)
                )
            }
            request = schemas.get("request")
            response = schemas.get("response")
            out.append(
                Endpoint(
                    method=r["method"],
                    host=r["host"],
                    path_template=r["url_template"],
                    path_params=[PathParam(**p) for p in json.loads(r["path_params"])],
                    query_params=[QueryParam(**p) for p in json.loads(r["query_params"])],
                    request_schema=request[0] if request else None,
                    response_schema=response[0] if response else None,
                    sample_count=r["sample_count"],
                    request_samples=request[1] if request else 0,
                    response_samples=response[1] if response else 0,
                    statuses=json.loads(r["statuses"]),
                    example_urls=json.loads(r["example_urls"]),
                    third_party=bool(r["third_party"]),
                    replayable=None if r["replayable"] is None else bool(r["replayable"]),
                    auth_type=r["auth_type"],
                    pagination=json.loads(r["pagination"]) if r["pagination"] else None,
                    required_headers=json.loads(r["required_headers"]),
                )
            )
        return out

    # --- probe results ---

    def save_probe_report(
        self, domain: str, run_id: int, endpoint: Endpoint, report: ProbeReport
    ) -> None:
        self.conn.execute(
            """UPDATE endpoints SET replayable = ?, auth_type = ?, pagination = ?,
                   required_headers = ?, updated_at = ?
               WHERE domain = ? AND method = ? AND host = ? AND url_template = ?""",
            (
                int(report.replayable),
                report.auth_type,
                json.dumps(report.pagination) if report.pagination else None,
                json.dumps(report.required_headers),
                _now(),
                domain,
                endpoint.method,
                endpoint.host,
                endpoint.path_template,
            ),
        )
        for h in report.hypotheses:
            self.conn.execute(
                """INSERT INTO hypotheses (run_id, endpoint_key, text, result, evidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, h.endpoint, h.text, h.result, h.evidence, _now()),
            )
        self.conn.commit()

    def hypotheses_for_run(self, run_id: int) -> list[dict[str, object]]:
        rows = self.conn.execute(
            "SELECT endpoint_key, text, result, evidence FROM hypotheses WHERE run_id = ?"
            " ORDER BY id",
            (run_id,),
        )
        return [dict(r) for r in rows]

    # --- agent steps ---

    def add_step(
        self,
        run_id: int,
        step_no: int,
        thought: str,
        action: str,
        args: dict[str, object],
        result: str,
        new_endpoints: int,
    ) -> None:
        self.conn.execute(
            """INSERT INTO agent_steps (run_id, step_no, thought, action, args, result,
                   new_endpoints, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, step_no, thought, action, json.dumps(args), result, new_endpoints, _now()),
        )
        self.conn.commit()

    def steps_for_run(self, run_id: int) -> list[dict[str, object]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY step_no", (run_id,)
        )
        return [{**dict(r), "args": json.loads(r["args"])} for r in rows]

    # --- LLM cache ---

    def cache_get(self, prompt_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT response FROM llm_cache WHERE prompt_hash = ?", (prompt_hash,)
        ).fetchone()
        return str(row["response"]) if row else None

    def cache_put(self, prompt_hash: str, provider: str, model: str, response: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO llm_cache (prompt_hash, provider, model, response, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (prompt_hash, provider, model, response, _now()),
        )
        self.conn.commit()
