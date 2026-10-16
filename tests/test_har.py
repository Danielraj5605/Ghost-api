"""Phase 4: map an API from a HAR file the user recorded in their browser."""

import base64
import json
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from ghost_api import exitcodes
from ghost_api.capture.har import read_har
from ghost_api.cli import app
from ghost_api.memory.store import Store
from tests.fake_secrets import SESSION_ID, bearer

runner = CliRunner()
EMAIL = "member.one" + "@" + "example.org"


def entry(url: str, body: str, *, kind: str | None = "fetch", status: int = 200,
          method: str = "GET", headers: dict[str, str] | None = None,
          mime: str = "application/json", base64_body: bool = False) -> dict[str, Any]:  # fmt: skip
    content: dict[str, Any] = {"mimeType": mime, "text": body}
    if base64_body:
        content = {"mimeType": mime, "encoding": "base64",
                   "text": base64.b64encode(body.encode()).decode()}  # fmt: skip
    item: dict[str, Any] = {
        "request": {"method": method, "url": url,
                    "headers": [{"name": k, "value": v} for k, v in (headers or {}).items()]},
        "response": {"status": status, "headers": [{"name": "Content-Type", "value": mime}],
                     "content": content},
    }  # fmt: skip
    if kind is not None:
        item["_resourceType"] = kind
    return item


def har(base: str, tmp_path: Path, fetch: bool = True) -> Path:
    def get(path: str) -> str:
        return httpx.get(base + path).text if fetch else json.dumps({"items": [{"id": 1}]})

    secret = {"Authorization": bearer(), "Cookie": f"session={SESSION_ID}"}
    entries = [
        entry(base + "/", "<html></html>", kind="document", mime="text/html"),
        entry(base + "/app.js", "console.log(1)", kind="script", mime="text/javascript"),
        entry(base + "/api/categories", get("/api/categories"), headers=secret),
        entry(base + "/api/products?page=1", get("/api/products?page=1"), kind=None),  # Firefox
        entry(base + "/api/products?page=2", get("/api/products?page=2"), base64_body=True),
        entry(base + "/api/members", json.dumps({"members": [{"id": 1, "email": EMAIL}]})),
        entry(base + "/old", "", status=302),
    ]
    path = tmp_path / "session.har"
    path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}), "utf-8")
    return path


def test_har_entries_are_filtered_like_live_traffic(tmp_path: Path) -> None:
    recording = read_har(har("https://shop.test", tmp_path, fetch=False))
    assert recording.start_url == "https://shop.test/" and recording.target_host == "shop.test"
    kept = [c.url.split("shop.test")[1] for c, _ in recording.captured]
    assert kept == ["/api/categories", "/api/products?page=1", "/api/products?page=2",
                    "/api/members"]  # fmt: skip
    assert recording.dropped["resource:script"] == 1 and recording.dropped["redirect"] == 1
    stored = json.dumps([c.__dict__ for c, _ in recording.captured], default=str)
    assert bearer().split()[1] not in stored and SESSION_ID not in stored and EMAIL not in stored
    raw = recording.captured[0][1]
    assert raw.headers["authorization"] == bearer()  # kept in memory only, for replays


def test_import_offline_maps_the_api_and_sends_nothing(tmp_path: Path) -> None:
    path = har("https://shop.test", tmp_path, fetch=False)
    db, out = tmp_path / "g.db", tmp_path / "out"
    result = runner.invoke(app, ["--json", "import", str(path), "--offline", "--db", str(db),
                                 "--out", str(out)])  # fmt: skip
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert {e["path"] for e in data["endpoints"]} >= {"/api/categories", "/api/products"}
    assert data["client"] is not None
    with Store(db) as store:
        text = json.dumps([r.__dict__ for r in store.requests_for_domain("shop.test")])
    assert EMAIL not in text and SESSION_ID not in text


def test_import_tests_endpoints_against_the_site(tmp_path: Path) -> None:
    from evals import demo_shop

    server = demo_shop.serve(0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        path = har(base, tmp_path)
        db, out = str(tmp_path / "g.db"), str(tmp_path / "out")
        result = runner.invoke(app, ["--json", "import", str(path), "--db", db, "--out", out])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        products = next(e for e in data["endpoints"] if e["path"] == "/api/products")
        assert products["pagination"]["style"] == "page" and products["status"] == "verified"
    finally:
        server.shutdown()


def test_a_file_that_is_not_har_is_a_usage_error(tmp_path: Path) -> None:
    bad = tmp_path / "x.har"
    bad.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["import", str(bad), "--db", str(tmp_path / "g.db")])
    assert result.exit_code == exitcodes.USAGE and "not a HAR file" in result.output
