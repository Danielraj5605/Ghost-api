"""Stage 9: confidence, OpenAPI export (validated), report.md, and the CLI commands."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from openapi_spec_validator import validate
from typer.testing import CliRunner

from ghost_api.analysis.analyzer import analyze
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.filters import REDACTED
from ghost_api.capture.models import CapturedRequest
from ghost_api.export import write_exports
from ghost_api.export.confidence import confidence
from ghost_api.export.openapi import build_openapi, to_yaml
from ghost_api.export.report import build_report
from ghost_api.generator.naming import apply_plan
from ghost_api.memory.store import Store
from ghost_api.probe.prober import Hypothesis, ProbeReport

HOST = "shop.test"
START = f"https://{HOST}/"


def cap(url: str, body: Any, method: str = "GET", req_body: Any = None) -> CapturedRequest:
    return CapturedRequest(
        method,
        url,
        200,
        "fetch",
        {},
        json.dumps(req_body) if req_body is not None else None,
        "application/json" if req_body is not None else None,
        "application/json",
        json.dumps(body),
    )


REQUESTS = [
    cap(
        f"https://{HOST}/api/items?page={p}&lang=en&api_key={REDACTED}",
        {"items": [{"id": p, "name": None if p == 1 else "x"}]},
    )
    for p in (1, 2, 3)
] + [
    cap(f"https://{HOST}/api/items/7", {"id": 7, "tags": ["a"]}),
    cap(f"https://{HOST}/api/me", {"name": "ada"}),
    cap(f"https://{HOST}/api/feed", {"posts": []}),
    cap(f"https://{HOST}/api/partner", {"ok": True}),
    cap(f"https://{HOST}/api/search", {"hits": []}, "POST", {"query": "dune", "page": 0}),
    cap("https://search.thirdparty.net/1/query", {"hits": [{"id": 1}]}),
    cap(f"https://{HOST}/api/items/7", {}, "DELETE"),
    cap(f"https://{HOST}/api/signed", {"x": 1}),
]


def endpoints() -> list[Endpoint]:
    eps = {e.path_template + e.method: e for e in analyze(REQUESTS)}
    eps["/api/itemsGET"].pagination = {
        "style": "page",
        "param": "page",
        "location": "query",
        "size_param": None,
        "confirmed": True,
    }
    eps["/api/meGET"].required_headers = ["cookie"]
    eps["/api/feedGET"].required_headers = ["Authorization"]
    eps["/api/partnerGET"].required_headers = ["x-partner"]
    eps["/api/signedGET"].replayable = False
    for ep in eps.values():
        if ep.replayable is None:
            ep.replayable = True
    return list(eps.values())


# --- confidence ---


@pytest.mark.parametrize(
    ("changes", "level"),
    [
        ({"status": "verified", "replayable": True, "sample_count": 5}, "High"),
        ({"status": "verified", "replayable": True, "sample_count": 1}, "Medium"),
        ({"replayable": True, "sample_count": 3}, "Medium"),
        ({"sample_count": 1}, "Low"),
        ({"status": "failed", "replayable": True, "sample_count": 9}, "Low"),
    ],
)
def test_confidence(changes: dict[str, Any], level: str) -> None:
    ep = Endpoint("GET", HOST, "/x")
    for k, v in changes.items():
        setattr(ep, k, v)
    assert confidence(ep) == level


# --- OpenAPI ---


def openapi_doc() -> dict[str, Any]:
    eps = endpoints()
    _, names = apply_plan(None, eps, HOST)
    return build_openapi(HOST, START, eps, names)


def test_openapi_is_valid() -> None:
    text = to_yaml(openapi_doc())
    validate(yaml.safe_load(text))  # raises if the document is not valid OpenAPI 3.1
    assert REDACTED not in text


def test_openapi_contents() -> None:
    doc = openapi_doc()
    paths = doc["paths"]
    assert "/api/signed" not in paths  # can't be replayed
    assert "delete" not in paths["/api/items/{id}"]  # never export data-changing calls

    items = paths["/api/items"]["get"]
    params = {p["name"]: p for p in items["parameters"]}
    assert params["page"]["required"] is False and params["page"]["schema"]["default"] == 1
    assert params["lang"]["schema"]["default"] == "en"
    assert "api_key" not in params  # a secret: becomes a security scheme
    assert items["security"] == [{"apiKey": []}]
    assert items["x-ghost-api"]["pagination"]["style"] == "page"

    schemes = doc["components"]["securitySchemes"]
    assert schemes["apiKey"] == {"type": "apiKey", "in": "query", "name": "api_key"}
    assert schemes["sessionCookie"]["in"] == "cookie"
    assert schemes["bearerAuth"] == {"type": "http", "scheme": "bearer"}
    assert {
        "name": "x-partner",
        "in": "header",
        "required": True,
        "schema": {"type": "string"},
    } in paths["/api/partner"]["get"]["parameters"]

    assert paths["/1/query"]["get"]["servers"] == [{"url": "https://search.thirdparty.net"}]
    body = paths["/api/search"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert body["properties"]["query"]["type"] == "string"
    ref = items["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    model = doc["components"]["schemas"][ref.rsplit("/", 1)[1]]
    name_type = model["properties"]["items"]["items"]["properties"]["name"]["type"]
    assert set(name_type) == {"null", "string"}  # JSON Schema type list: needs OpenAPI 3.1


# --- report ---


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with Store(tmp_path / "t.db") as s:
        yield s


def populate(store: Store) -> int:
    run_id = store.create_run(HOST, START, "list items | tags")
    for req in REQUESTS[:4]:
        store.add_request(run_id, req)
    eps = analyze(REQUESTS[:4])
    store.save_endpoints(HOST, run_id, eps)
    store.add_step(
        run_id,
        1,
        "Scroll to load more",
        "scroll",
        {"direction": "bottom"},
        "1 new request: GET /api/items?page=2",
        1,
    )
    items = next(e for e in eps if e.path_template == "/api/items")
    report = ProbeReport(
        items.key,
        replayable=True,
        auth_type="api-key-query",
        pagination={
            "style": "page",
            "param": "page",
            "location": "query",
            "size_param": None,
            "confirmed": True,
        },
        hypotheses=[
            Hypothesis(items.key, "paginates with 'page'", "confirmed", "3 different"),
            Hypothesis(items.key, "header 'x' is required", "inconclusive", "HTTP 500"),
        ],
    )
    store.save_probe_report(HOST, run_id, items, report)
    store.set_endpoint_status(HOST, items, "verified", "2 live test(s) passed")
    detail = next(e for e in eps if e.path_template == "/api/items/{id}")
    store.set_endpoint_status(HOST, detail, "failed", "the API said not found (HTTP 404)")
    store.add_lessons(
        HOST, run_id, ["GET /api/items appeared after scroll(direction='bottom')"], "auto"
    )
    store.record_usage(run_id, 3, 2500)
    store.finish_run(run_id, "goal-covered")
    return run_id


def test_report_matches_the_database(store: Store) -> None:
    run_id = populate(store)
    text = build_report(store, HOST, run_id)
    assert "# Ghost API report: shop.test" in text
    assert "Goal: _list items \\| tags_" in text  # pipes escaped
    assert "- **Outcome:** goal-covered" in text
    assert "- **Endpoints:** 2 (1 verified, 0 unverified, 1 failed)" in text
    assert "3 LLM calls, 2,500 tokens, 4 captured requests" in text
    assert "| `GET /api/items` | verified | High | 3 | api-key-query | page: `page` |" in text
    assert "| `GET /api/items/{id}` | failed | Low |" in text
    assert "- confirmed: paginates with 'page' (3 different)" in text
    assert "1. `scroll(direction='bottom')`: Scroll to load more" in text
    assert "- Inconclusive: header 'x' is required (HTTP 500)" in text
    assert "is failed: the API said not found (HTTP 404)" in text
    assert "## Lessons remembered for next time" in text


def test_report_for_an_unfinished_run(store: Store) -> None:
    run_id = store.create_run(HOST, START, "goal")
    store.save_endpoints(HOST, run_id, analyze(REQUESTS[:1]))
    text = build_report(store, HOST, run_id)
    assert "did not finish (stopped unexpectedly)" in text
    assert "has not been verified yet" in text
    assert "The agent was not used in this run." in text


def test_write_exports(store: Store, tmp_path: Path) -> None:
    run_id = populate(store)
    exports = write_exports(store, HOST, run_id, tmp_path / "out")
    validate(yaml.safe_load(exports.openapi.read_text(encoding="utf-8")))
    assert exports.report.read_text(encoding="utf-8").startswith("# Ghost API report")
    store.save_naming_plan(HOST, "{not json")  # a corrupt plan must not break exports
    write_exports(store, HOST, run_id, tmp_path / "out")


# --- CLI ---


def test_report_and_inspect_run_commands(store: Store, tmp_path: Path) -> None:
    from ghost_api.cli import app

    run_id = populate(store)
    db = tmp_path / "t.db"
    runner = CliRunner()
    result = runner.invoke(app, ["report", HOST, "--db", str(db), "--out", str(tmp_path / "o")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "o" / HOST / "openapi.yaml").exists()

    result = runner.invoke(app, ["inspect", "--run", str(run_id), "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "Step 1" in result.output and "scroll" in result.output
    assert "paginates with 'page'" in result.output

    result = runner.invoke(app, ["report", "other.com", "--db", str(db)])
    assert result.exit_code == 1 and "No run found" in result.output
