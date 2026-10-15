"""`ghost-api mock`: a local copy of a site's API from captured replies."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from ghost_api.analysis.analyzer import analyze
from ghost_api.capture.models import CapturedRequest
from ghost_api.memory.store import Store
from ghost_api.serve.mock import build_routes, create_mock_app


def cap(url: str, body: object, method: str = "GET") -> CapturedRequest:
    return CapturedRequest(method, url, 200, "fetch", {}, None, None, "application/json",
                           json.dumps(body))  # fmt: skip


def app_for(tmp_path: Path) -> TestClient:
    reqs = [cap(f"https://ex.com/api/quotes?page={i}", {"page": i, "quotes": [i]}) for i in (1, 2)]
    reqs += [cap(f"https://ex.com/api/authors/{a}", {"id": a}) for a in (11, 12, 13, 14)]
    with Store(tmp_path / "g.db") as store:
        run = store.create_run("ex.com", "https://ex.com/", "quotes")
        for r in reqs:
            store.add_request(run, r)
        store.save_endpoints("ex.com", run, analyze(reqs))
        routes = build_routes(store, "ex.com")
    return TestClient(create_mock_app(routes, "ex.com", {"testserver"}))


def test_mock_replays_the_closest_captured_reply(tmp_path: Path) -> None:
    client = app_for(tmp_path)
    exact = client.get("/api/quotes", params={"page": 2})
    assert exact.json() == {"page": 2, "quotes": [2]} and exact.headers["x-ghost-mock"] == "exact"
    closest = client.get("/api/quotes", params={"page": 9})
    assert closest.status_code == 200 and closest.headers["x-ghost-mock"] == "closest"
    assert client.get("/api/authors/12").json() == {"id": 12}
    assert client.get("/api/authors/99").status_code == 200  # same endpoint, a captured reply


def test_mock_explains_unknown_paths_and_allows_browsers(tmp_path: Path) -> None:
    client = app_for(tmp_path)
    missing = client.get("/api/nope")
    assert missing.status_code == 404 and "GET /api/quotes" in missing.json()["known"]
    preflight = client.options("/api/quotes")
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "*"
    routes = client.get("/__ghost/routes").json()["routes"]
    assert len(routes) == 2 and "/api/quotes" in {r["path"] for r in routes}


def test_mock_refuses_other_host_names(tmp_path: Path) -> None:
    client = app_for(tmp_path)
    assert client.get("/api/quotes", headers={"host": "evil.example"}).status_code == 403
