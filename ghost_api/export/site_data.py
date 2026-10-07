"""What is stored about a site, as plain JSON for the dashboard and report.html.

The dashboard's API and the shareable report use these same shapes, so the React
components that show them work in both places (see web/src/types.ts).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ghost_api import __version__
from ghost_api.analysis.models import Endpoint
from ghost_api.analysis.schema import field_paths, top_level_fields
from ghost_api.export.confidence import confidence
from ghost_api.memory.store import RunRow, Store

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

# Code and spec files embedded in report.html (bigger files are listed, not embedded).
MAX_EMBEDDED_FILE = 300_000
EMBEDDED_KINDS = ("client", "models", "tests", "openapi")
# Left out of the shared report: request events (many, with full URLs), endpoint lists
# (the report has its own, with sensitive examples removed) and screenshots (not embedded).
SKIPPED_EVENTS = {"request", "endpoints", "screenshot"}


def display_label(ep: Endpoint, host: str) -> str:
    return ep.label if ep.host == host else f"{ep.method} {ep.host}{ep.path_template}"


def endpoint_summary(ep: Endpoint, host: str) -> dict[str, Any]:
    """Everything an interface needs to show an endpoint, as plain JSON."""
    params = [
        {
            "name": p.name,
            "type": p.type,
            "location": "path",
            "kind": "required",
            "examples": p.examples[:3],
            "sensitive": False,
        }
        for p in ep.path_params
    ] + [
        {
            "name": p.name,
            "type": p.type,
            "location": "query",
            "kind": p.kind,
            "examples": p.examples[:3],
            "sensitive": p.sensitive,
        }
        for p in ep.query_params
    ]
    return {
        "key": ep.key,
        "label": display_label(ep, host),
        "method": ep.method,
        "host": ep.host,
        "path": ep.path_template,
        "third_party": ep.host != host,
        "params": params,
        "samples": ep.sample_count,
        "statuses": ep.statuses,
        "status": ep.status,
        "status_note": ep.status_note,
        "replayable": ep.replayable,
        "auth_type": ep.auth_type,
        "required_headers": ep.required_headers,
        "pagination": ep.pagination,
        "confidence": confidence(ep),
        "fields": top_level_fields(ep.response_schema),
        "field_paths": field_paths(ep.response_schema, limit=40),
        "has_request_body": ep.request_schema is not None,
    }


def endpoint_details(store: Store, domain: str) -> list[dict[str, Any]]:
    """Summaries plus schemas and example URLs, from the latest run that found endpoints."""
    run_id = store.latest_endpoint_run(domain)
    if run_id is None:
        return []
    details = []
    for ep in store.load_endpoints(domain, run_id):
        summary = endpoint_summary(ep, domain)
        summary.update(
            request_schema=ep.request_schema,
            response_schema=ep.response_schema,
            example_urls=ep.example_urls,
        )
        details.append(summary)
    return details


def run_dict(r: RunRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "domain": r.domain,
        "start_url": r.start_url,
        "goal": r.goal,
        "status": r.status,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "requests": r.request_count,
        "llm_calls": r.llm_calls,
        "tokens": r.tokens,
    }


def file_kind(name: str) -> str:
    if name.startswith("test_") or name.endswith(".test.ts"):
        return "tests"
    if name.endswith(("_client.py", "_client.ts")):
        return "client"
    if name == "models.py":
        return "models"
    if name.endswith((".yaml", ".yml")):
        return "openapi"
    if name.startswith("report."):
        return "report"
    return "other"


def site_files(folder: Path) -> list[dict[str, Any]]:
    if not folder.is_dir():
        return []
    return [
        {"name": f.name, "size": f.stat().st_size, "kind": file_kind(f.name)}
        for f in sorted(folder.iterdir())
        if f.is_file()
    ]


def _public_endpoint(ep: dict[str, Any]) -> dict[str, Any]:
    """Drop example values of sensitive parameters before sharing."""
    params = [{**p, "examples": []} if p["sensitive"] else p for p in ep["params"]]
    return {**ep, "params": params}


def report_data(
    store: Store, domain: str, run_id: int, folder: Path, report_md: str, generated_at: str
) -> dict[str, Any]:
    """Everything report.html shows. It is meant to be shared, so it holds no secrets:
    stored requests are already redacted, sensitive examples are dropped, and request
    events (full URLs) are left out."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    files = []
    for f in site_files(folder):
        if f["kind"] not in EMBEDDED_KINDS:
            continue
        path = folder / f["name"]
        content = (
            path.read_text(encoding="utf-8", errors="replace")
            if f["size"] <= MAX_EMBEDDED_FILE
            else None
        )
        files.append({**f, "content": content})
    events = [e for e in store.events_for_run(run_id) if e.get("type") not in SKIPPED_EVENTS]
    return {
        "version": __version__,
        "generated_at": generated_at,
        "domain": domain,
        "run": run_dict(run),
        "endpoints": [_public_endpoint(e) for e in endpoint_details(store, domain)],
        "events": events,
        "lessons": store.lessons_for(domain, limit=20),
        "files": files,
        "report_md": report_md,
    }
