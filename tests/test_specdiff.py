"""`ghost-api verify-spec` (docs vs reality) and `ghost-api explain`."""

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from ghost_api import exitcodes
from ghost_api.analysis.analyzer import analyze
from ghost_api.capture.models import CapturedRequest
from ghost_api.cli import app
from ghost_api.memory.store import Store
from ghost_api.specdiff import compare

runner = CliRunner()


def cap(url: str, body: object) -> CapturedRequest:
    return CapturedRequest("GET", url, 200, "fetch", {}, None, None, "application/json",
                           json.dumps(body))  # fmt: skip


def stored(db: Path) -> None:
    reqs = [cap(f"https://ex.com/api/items?page={p}", {"page": p, "items": [], "total": 3})
            for p in (1, 2, 3)]  # fmt: skip
    reqs += [cap(f"https://ex.com/api/items/{i}", {"id": i, "name": "n", "rating": 4})
             for i in (11, 12, 13, 14)]  # fmt: skip
    reqs += [cap("https://ex.com/api/hidden-stats", {"visits": 9})]
    with Store(db) as store:
        run = store.create_run("ex.com", "https://ex.com/", "items")
        for r in reqs:
            store.add_request(run, r)
        store.save_endpoints("ex.com", run, analyze(reqs))


SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Ex", "version": "1"},
    "paths": {
        "/api/items": {"get": {"responses": {"200": {"content": {"application/json": {
            "schema": {"type": "object",
                       "properties": {"page": {}, "items": {}, "total": {}}}}}}}}},
        "/api/items/{itemId}": {"get": {"responses": {"200": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Item"}}}}}}},
        "/api/legacy": {"get": {"responses": {"200": {"description": "old"}}}},
    },
    "components": {"schemas": {"Item": {"type": "object", "properties": {
        "id": {}, "name": {}, "price": {}}}}},
}  # fmt: skip


def test_docs_are_compared_with_reality(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    stored(db)
    with Store(db) as store:
        checks = {c.endpoint: c for c in compare(SPEC, store.current_endpoints("ex.com"))}
    assert checks["GET /api/items"].status == "documented"
    assert checks["GET /api/items"].extra_fields == []
    item = checks["GET /api/items/{}"]
    assert item.extra_fields == ["rating"] and item.missing_fields == ["price"]
    assert checks["GET /api/legacy"].status == "not seen"
    assert checks["GET /api/hidden-stats"].status == "undocumented"


def test_verify_spec_and_explain_commands(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    stored(db)
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    result = runner.invoke(app, ["--json", "verify-spec", str(spec_file), "ex.com",
                                 "--db", str(db)])  # fmt: skip
    assert result.exit_code == 0, result.output
    statuses = {e["endpoint"]: e["status"] for e in json.loads(result.stdout)["endpoints"]}
    assert statuses["GET /api/hidden-stats"] == "undocumented"

    bad = tmp_path / "bad.yaml"
    bad.write_text("just: text", encoding="utf-8")
    refused = runner.invoke(app, ["verify-spec", str(bad), "ex.com", "--db", str(db)])
    assert refused.exit_code == exitcodes.USAGE and "not an OpenAPI document" in refused.output

    explained = runner.invoke(app, ["explain", "ex.com", "--db", str(db)])
    assert explained.exit_code == 0, explained.output
    assert "It has 3 API endpoints; none needs a login." in explained.output
    assert "GET /api/items" in explained.output and "ghost-api fetch ex.com" in explained.output
