"""Phase 3: the dashboard's API (FastAPI TestClient; no LLM quota)."""

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from evals.demo_shop import serve as serve_shop
from ghost_api.web import server
from ghost_api.web.server import RunManager, create_app

TOKEN = "fake-removed"
AUTH = {"x-ghost-token": TOKEN}


@pytest.fixture
def shop() -> Iterator[str]:
    httpd = serve_shop(0)
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    db, out = tmp_path / "t.db", tmp_path / "out"
    manager = RunManager(db, out, gateway_factory=lambda calls: None)
    client = TestClient(create_app(db, out, TOKEN, manager=manager))
    return {"db": db, "out": out, "client": client, "manager": manager}


def start_run(env: dict[str, Any], url: str) -> int:
    resp = env["client"].post(
        "/api/runs", headers=AUTH, json={"url": url, "goal": "list products", "agent": False}
    )
    assert resp.status_code == 200, resp.text
    run_id: int = resp.json()["run_id"]
    env["manager"].wait(300)
    return run_id


# --- guards ---


def test_api_needs_the_token(env: dict[str, Any]) -> None:
    c = env["client"]
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers={"x-ghost-token": "fake-removed"}).status_code == 401
    assert c.get("/api/state", headers=AUTH).json()["version"]
    assert c.get(f"/api/state?token={TOKEN}").status_code == 200  # EventSource / <img> form


def test_unknown_host_and_cross_origin_writes_are_refused(tmp_path: Path) -> None:
    app = create_app(tmp_path / "t.db", tmp_path / "out", TOKEN, allowed_hosts={"127.0.0.1:8800"})
    c = TestClient(app, base_url="http://127.0.0.1:8800")
    assert c.get("/api/state", headers=AUTH).status_code == 200
    evil = TestClient(app, base_url="http://evil.example:8800")
    assert evil.get("/api/state", headers=AUTH).status_code == 403  # DNS rebinding
    resp = c.post("/api/runs/1/stop", headers={**AUTH, "origin": "http://evil.example"})
    assert resp.status_code == 403


# --- a full run through the API ---


def test_run_from_the_api_and_follow_it_live(env: dict[str, Any], shop: str) -> None:
    c = env["client"]
    run_id = start_run(env, shop)

    with c.stream("GET", f"/api/runs/{run_id}/events?token={TOKEN}") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith('data: {"')]
    types = [e["type"] for e in events]
    assert types[0] == "run_started" and types[1] == "notice" and "run_finished" in types
    assert any(e["type"] == "hypothesis" and e["result"] == "confirmed" for e in events)
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)

    sites = c.get("/api/sites", headers=AUTH).json()
    site = next(s for s in sites if s["domain"] == "127.0.0.1")
    assert site["endpoints"] >= 2 and site["has_client"]

    detail = c.get("/api/sites/127.0.0.1", headers=AUTH).json()
    products = next(e for e in detail["endpoints"] if e["label"] == "GET /api/products")
    assert products["response_schema"]["type"] == "object"
    assert products["pagination"]["confirmed"] is True

    run = c.get(f"/api/runs/{run_id}", headers=AUTH).json()
    assert run["status"] == "captured" and run["hypotheses"]
    shot = next(e for e in events if e["type"] == "screenshot")
    img = c.get(f"/api/runs/{run_id}/screens/{shot['name']}?token={TOKEN}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

    # Try it: a read-only call with changed parameters.
    tried = c.post(
        "/api/sites/127.0.0.1/try",
        headers=AUTH,
        json={"endpoint": products["key"], "params": {"page": "2"}},
    )
    assert tried.status_code == 200, tried.text
    body = tried.json()
    assert body["status"] == 200 and json.loads(body["body"])["page"] == 2

    # Regenerate as TypeScript.
    ts = c.post("/api/sites/127.0.0.1/generate", headers=AUTH, json={"lang": "typescript"})
    assert ts.status_code == 200 and ts.json()["lang"] == "typescript"

    # Files, one file, and the zip.
    names = {f["name"]: f["kind"] for f in c.get("/api/sites/127.0.0.1/files", headers=AUTH).json()}
    assert "openapi" in names.values() and "client" in names.values()
    report = c.get("/api/sites/127.0.0.1/files/report.md", headers=AUTH)
    assert report.status_code == 200 and report.text.startswith("# Ghost API report")
    archive = c.get(f"/api/sites/127.0.0.1/download.zip?token={TOKEN}")
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        assert "127.0.0.1/report.md" in zf.namelist()


def test_only_one_run_at_a_time_and_bad_input(env: dict[str, Any]) -> None:
    c = env["client"]
    bad = c.post("/api/runs", headers=AUTH, json={"url": "ftp://x", "goal": "g", "agent": False})
    assert bad.status_code == 400 and "valid http" in bad.json()["detail"]
    no_llm = c.post(
        "/api/runs",
        headers=AUTH,
        json={"url": "https://x.test", "goal": "g", "ignore_robots": True},
    )
    assert no_llm.status_code == 400 and "No LLM API key" in no_llm.json()["detail"]
    assert (
        c.post(
            "/api/runs", headers=AUTH, json={"url": "https://x.test", "goal": "g", "lang": "cobol"}
        ).status_code
        == 400
    )


# --- files and names are confined to the output folder ---


@pytest.mark.parametrize(
    "path",
    [
        "/api/sites/..%2F..%2Fetc/files",
        "/api/sites/127.0.0.1/files/..%2F..%2Fsecret.txt",
        "/api/sites/127.0.0.1/files/.env",
        "/api/runs/1/screens/..%2Fx.jpg",
        "/api/runs/1/screens/x.png",
    ],
)
def test_path_traversal_is_refused(env: dict[str, Any], path: str) -> None:
    (env["out"] / "127.0.0.1").mkdir(parents=True)
    (env["out"] / "secret.txt").write_text("no", encoding="utf-8")
    resp = env["client"].get(path, headers=AUTH)
    assert resp.status_code in (400, 404)
    assert resp.text != "no"


def test_try_refuses_endpoints_that_change_data(env: dict[str, Any]) -> None:
    from ghost_api.analysis.analyzer import analyze
    from ghost_api.capture.models import CapturedRequest
    from ghost_api.memory.store import Store

    cart = CapturedRequest(
        "POST",
        "https://shop.test/api/cart",
        200,
        "fetch",
        {},
        '{"id": 1}',
        "application/json",
        "application/json",
        '{"ok": true}',
    )
    with Store(env["db"]) as s:
        run_id = s.create_run("shop.test", "https://shop.test/", "g")
        s.add_request(run_id, cart)
        s.save_endpoints("shop.test", run_id, analyze([cart]))
        key = s.load_endpoints("shop.test", run_id)[0].key
    resp = env["client"].post(
        "/api/sites/shop.test/try", headers=AUTH, json={"endpoint": key, "params": {}}
    )
    assert resp.status_code == 409 and "changes data" in resp.json()["detail"]


# --- the frontend page ---


def test_index_gets_the_token(
    env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        '<script>window.GHOST_TOKEN="__GHOST_TOKEN__"</script>', encoding="utf-8"
    )
    monkeypatch.setattr(server, "STATIC", static)
    page = env["client"].get("/sites/quotes.toscrape.com")  # client-side route
    assert page.status_code == 200 and f'"{TOKEN}"' in page.text


def test_missing_frontend_explains_how_to_build(
    env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "STATIC", tmp_path / "nothing")
    page = env["client"].get("/")
    assert page.status_code == 503 and "npm run build" in page.text


def test_doctor(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "provider": "gemini",
            "model": "m",
            "key": True,
            "ok": True,
            "detail": "OK",
            "seconds": 0.5,
        }
    ]
    monkeypatch.setattr(server, "check_providers", lambda: rows)
    assert env["client"].post("/api/doctor", headers=AUTH).json() == rows
