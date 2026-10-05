"""Write report.md and openapi.yaml for a site (Stage 9)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ghost_api.export.openapi import build_openapi, to_yaml
from ghost_api.export.report import build_report
from ghost_api.generator.naming import NamingPlan, apply_plan
from ghost_api.generator.spec import ClientSpec
from ghost_api.memory.store import Store


@dataclass
class Exports:
    report: Path
    openapi: Path


def write_exports(
    store: Store,
    domain: str,
    run_id: int,
    out_root: Path,
    spec: ClientSpec | None = None,
) -> Exports:
    """Write both files into out_root/<domain>/ from what the database holds."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    endpoints = store.load_endpoints(domain, store.latest_endpoint_run(domain))
    plan = None
    stored_plan = store.load_naming_plan(domain)
    if stored_plan:
        try:
            plan = NamingPlan.model_validate_json(stored_plan)
        except ValueError:
            plan = None  # fall back to URL-based names
    _, names = apply_plan(plan, endpoints, domain)

    folder = out_root / domain
    folder.mkdir(parents=True, exist_ok=True)
    openapi_path = folder / "openapi.yaml"
    openapi_path.write_text(
        to_yaml(build_openapi(domain, run.start_url, endpoints, names)), encoding="utf-8"
    )
    report_path = folder / "report.md"
    report_path.write_text(build_report(store, domain, run_id, spec), encoding="utf-8")
    return Exports(report_path, openapi_path)
