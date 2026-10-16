"""Stage 8: lessons, memory in prompts, naming-plan reuse, and `ghost-api refresh`."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from typer.testing import CliRunner

from ghost_api.analysis.analyzer import analyze
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.filters import REDACTED
from ghost_api.capture.models import CapturedRequest
from ghost_api.generator import plan_for
from ghost_api.generator.naming import EndpointNames, NamingPlan
from ghost_api.llm.gateway import LLMGateway
from ghost_api.llm.providers import Provider
from ghost_api.memory.knowledge import (
    MAX_LESSON_CHARS,
    clean_lesson,
    discovery_lessons,
    load_memory,
    memory_prompt,
    rebuild_raw,
    write_lessons,
)
from ghost_api.memory.store import Store
from tests.fake_secrets import fake_jwt


def cap(url: str, body: Any, headers: dict[str, str] | None = None) -> CapturedRequest:
    return CapturedRequest(
        "GET", url, 200, "fetch", headers or {}, None, None, "application/json", json.dumps(body)
    )


# --- lessons ---


def test_clean_lesson() -> None:
    jwt = fake_jwt()
    text = clean_lesson(f"Search\n  fires after typing; token {jwt} " + "x" * 500)
    assert "\n" not in text and jwt not in text
    assert text.startswith("Search fires after typing") and len(text) == MAX_LESSON_CHARS


def test_discovery_lessons() -> None:
    lessons = discovery_lessons(
        [
            ("scroll", {"direction": "bottom"}, ["GET /api/quotes"]),
            ("click", {"element_id": 4}, []),
            ("type", {"element_id": 2, "text": "dune", "submit": True}, ["GET /api/search"]),
            ("finish", {"goal_covered": True}, ["GET /x"]),
        ]
    )
    assert lessons == [
        "GET /api/quotes appeared after scroll(direction='bottom')",
        "GET /api/search appeared after type(text='dune', submit=True)",
    ]


def test_memory_prompt() -> None:
    assert memory_prompt([], []) == ""
    ep = Endpoint("GET", "ex.com", "/api/quotes", status="verified")
    text = memory_prompt([ep], ["Search needs 3 letters"])
    assert "may be outdated" in text
    assert "known endpoint: GET ex.com/api/quotes [verified]" in text
    assert "note: Search needs 3 letters" in text


def test_write_lessons_with_a_fake_llm() -> None:
    reply = {"lessons": ["The search box\nfires /api/search", "Tags load on scroll"]}

    def llm(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    gateway = LLMGateway(
        [Provider("fake", "https://llm.test/v1", "m", api_key="k")],
        client=httpx.Client(transport=httpx.MockTransport(llm)),
    )
    lessons = write_lessons(gateway, "goal", "goal-covered", [("click", {}, "ok")], [])
    assert lessons == ["The search box fires /api/search", "Tags load on scroll"]
    assert write_lessons(LLMGateway([]), "g", "s", [], []) == []  # no LLM: no lessons


# --- rebuilding requests from redacted samples ---


def test_rebuild_raw() -> None:
    public = cap("https://ex.com/api/items?page=1", {}, {"accept": "*/*", "cookie": REDACTED})
    raw = rebuild_raw(public, required_headers=[])
    assert raw is not None and raw.headers == {"accept": "*/*"}  # unneeded cookie dropped
    assert rebuild_raw(public, required_headers=["cookie"]) is None  # needed but unknown
    keyed = cap(f"https://ex.com/api/items?api_key={REDACTED}", {})
    assert rebuild_raw(keyed, required_headers=[]) is None


# --- store, memory loading and naming-plan reuse ---


def test_store_lessons_and_memory(tmp_path: Path) -> None:
    with Store(tmp_path / "t.db") as store:
        run_id = store.create_run("ex.com", "https://ex.com/", "list items")
        reqs = [cap("https://ex.com/api/items?page=1", {"items": [1]})]
        store.save_endpoints("ex.com", run_id, analyze(reqs))
        store.record_usage(run_id, 7, 100)
        assert store.add_lessons("ex.com", run_id, ["a", "b", "a"], "auto") == 2
        assert store.add_lessons("ex.com", run_id, ["b"], "llm") == 0  # duplicate
        refresh_id = store.create_run("ex.com", "https://ex.com/", "refresh: list items")
        store.record_usage(refresh_id, 0, 0)

        assert store.lessons_for("ex.com") == ["b", "a"]
        assert store.first_run_llm_calls("ex.com") == 7  # refreshes don't count
        mem = load_memory(store, "ex.com")
        assert mem is not None
        assert (mem.goal, mem.run_id, mem.lessons) == ("list items", run_id, ["b", "a"])
        assert [e.label for e in mem.endpoints] == ["GET /api/items"]
        assert load_memory(store, "unknown.com") is None


def test_plan_is_reused_until_endpoints_change(tmp_path: Path) -> None:
    calls = {"n": 0}
    eps = analyze([cap("https://ex.com/api/items", {"items": []})])

    def llm(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        keys = json.loads(request.content)["messages"][-1]["content"]
        plan = {
            "client_class": "ExClient",
            "endpoints": [
                {
                    "key": e.key,
                    "method_name": "list_items",
                    "response_model": "Items",
                    "summary": "s",
                }
                for e in eps2
                if e.key in keys
            ],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan)}}]})

    gateway = LLMGateway(
        [Provider("fake", "https://llm.test/v1", "m", api_key="k")],
        client=httpx.Client(transport=httpx.MockTransport(llm)),
    )
    eps2 = eps + [Endpoint("GET", "ex.com", "/api/tags")]
    with Store(tmp_path / "t.db") as store:
        first = plan_for(store, gateway, "ex.com", eps)
        again = plan_for(store, gateway, "ex.com", eps)
        assert first == again and calls["n"] == 1  # reused, no second call
        plan_for(store, gateway, "ex.com", eps2)  # a new endpoint: ask again
        assert calls["n"] == 2
    assert isinstance(first, NamingPlan) and isinstance(first.endpoints[0], EndpointNames)


# --- `ghost-api refresh` end to end ---


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":  # the page fetches the keyed API with a fresh key
            self._send(
                200,
                "text/html",
                b"<html><body><script>fetch('/api/secure?api_key=FRESH');</script></body></html>",
            )
        elif url.path == "/api/items":
            page = int(q.get("page", "1"))
            items = [{"id": page * 10 + i} for i in range(2)] if page <= 3 else []
            self._send(200, "application/json", json.dumps({"page": page, "items": items}).encode())
        elif url.path == "/api/secure":
            ok = q.get("api_key") == "FRESH"
            body = {"items": [{"id": 1}]} if ok else {"error": "key"}
            self._send(200 if ok else 401, "application/json", json.dumps(body).encode())
        else:
            self._send(404, "text/plain", b"not found")


@pytest.fixture
def site() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def first_run(db: Path, site: str, requests: list[CapturedRequest]) -> None:
    """What an earlier `ghost-api run` would have stored."""
    with Store(db) as store:
        run_id = store.create_run("127.0.0.1", f"{site}/", "list items")
        for req in requests:
            store.add_request(run_id, req)
        store.save_endpoints("127.0.0.1", run_id, analyze(requests))
        store.record_usage(run_id, 4, 3000)
        store.add_lessons("127.0.0.1", run_id, ["GET /api/items appeared after scroll"], "auto")
        store.finish_run(run_id, "goal-covered")


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    # tests/conftest.py removes every LLM key, so refreshes here never call a provider.
    return CliRunner()


def test_refresh_from_memory_needs_no_browser_and_no_llm(
    site: str, tmp_path: Path, cli: CliRunner
) -> None:
    from ghost_api.cli import app

    db = tmp_path / "t.db"
    first_run(db, site, [cap(f"{site}/api/items?page=1", {"page": 1, "items": [{"id": 10}]})])
    result = cli.invoke(
        app,
        [
            "refresh",
            "127.0.0.1",
            "--db",
            str(db),
            "--out",
            str(tmp_path / "out"),
            "--rate-limit",
            "50",
        ],
    )
    out = result.output
    assert result.exit_code == 0, out
    assert "1 of 1 endpoint(s) can be re-checked from stored samples" in out
    assert "Visiting the site" not in out  # step 2 not needed
    assert "Memory: 0 LLM call(s) vs 4 on the first run (100% fewer)" in out
    with Store(db) as store:
        (ep,) = store.load_endpoints("127.0.0.1", store.latest_endpoint_run("127.0.0.1"))
        assert ep.status == "verified"
        assert ep.pagination is not None and ep.pagination["confirmed"]
        assert store.list_runs("127.0.0.1")[0].goal == "refresh: list items"


def test_refresh_visits_the_site_when_a_key_must_be_fresh(
    site: str, tmp_path: Path, cli: CliRunner
) -> None:
    from ghost_api.cli import app

    db = tmp_path / "t.db"
    stored = cap(f"{site}/api/secure?api_key={REDACTED}", {"items": [{"id": 1}]})
    first_run(db, site, [stored])
    result = cli.invoke(
        app,
        [
            "refresh",
            "127.0.0.1",
            "--db",
            str(db),
            "--out",
            str(tmp_path / "out"),
            "--rate-limit",
            "50",
        ],
    )
    out = result.output
    assert result.exit_code == 0, out
    assert "0 of 1 endpoint(s) can be re-checked" in out
    assert "Visiting the site once for fresh traffic: GET /api/secure" in out
    assert "all known endpoints re-captured" in out
    with Store(db) as store:
        (ep,) = store.load_endpoints("127.0.0.1", store.latest_endpoint_run("127.0.0.1"))
        assert ep.status == "verified"
        assert ep.auth_type == "api-key-query"
        stored_text = json.dumps([r.url for r in store.requests_for_domain("127.0.0.1")])
        assert "FRESH" not in stored_text  # the fresh key was used, never stored
