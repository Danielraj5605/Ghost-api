"""End-to-end: the agent drives a real browser over a local single-page app.

The LLM is scripted (it picks elements by label from the prompt), so no quota is used.
"""

import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from ghost_api.agent.orchestrator import Explorer
from ghost_api.agent.tools import Decision
from ghost_api.analysis.analyzer import endpoint_keys
from ghost_api.browser.driver import BrowserSession
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.llm.gateway import LLMReply
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.probe.prober import Prober
from ghost_api.probe.replay import Replayer

APP = b"""<!doctype html><html><head><title>Book Finder</title></head><body><main>
<h1>Book Finder</h1>
<input type="search" id="q" placeholder="Search books">
<input type="password" placeholder="Password">
<button id="next">Next page</button>
<button id="delete" onclick="fetch('/api/account/delete', {method: 'POST'})">Delete account</button>
<a href="https://example.org/elsewhere">Partner site</a>
<a href="/about" target="_blank">About us</a>
<div id="results"></div>
<script>
  let page = 1, query = '';
  async function search() {
    const r = await fetch(`/api/search?q=${encodeURIComponent(query)}&page=${page}`);
    const data = await r.json();
    document.getElementById('results').innerHTML = data.results
      .map(b => `<button onclick="openBook(${b.id})">Open ${b.title}</button>`).join('');
  }
  async function openBook(id) { await fetch(`/api/books/${id}`); }
  document.getElementById('q').addEventListener('keydown', e => {
    if (e.key === 'Enter') { query = e.target.value; page = 1; search(); }
  });
  document.getElementById('next').onclick = () => { page += 1; search(); };
</script></main></body></html>"""

ABOUT = b"""<!doctype html><html><head><title>About</title></head><body>
<p>About</p><script>fetch('/api/team');</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *args: object) -> None:
        pass

    def _json(self, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def _html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        Handler.hits.append(f"GET {url.path}")
        if url.path == "/":
            self._html(APP)
        elif url.path == "/about":
            self._html(ABOUT)
        elif url.path == "/api/search":
            page = int(parse_qs(url.query).get("page", ["1"])[0])
            ids = [page * 10 + i for i in range(2)]
            self._json({"page": page, "results": [{"id": i, "title": f"Dune {i}"} for i in ids]})
        elif url.path.startswith("/api/books/"):
            self._json({"id": int(url.path.rsplit("/", 1)[1]), "title": "Dune", "pages": 412})
        elif url.path == "/api/team":
            self._json([{"name": "Ada"}])
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        Handler.hits.append(f"POST {urlsplit(self.path).path}")
        self._json({"ok": True})


@pytest.fixture
def server() -> Iterator[str]:
    Handler.hits = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


class LabelPickingLLM:
    """Follows a plan of (action, label, extra) and finds element ids in the prompt."""

    def __init__(self, plan: list[tuple[str, str, dict[str, Any]]]) -> None:
        self.plan = plan
        self.prompts: list[str] = []

    def ask(self, messages: list[dict[str, str]], response_model: Any = None,
            temperature: float = 0.0) -> LLMReply[Decision]:  # fmt: skip
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        action, label, extra = self.plan.pop(0)
        data: dict[str, Any] = {"thought": f"{action} {label}", "action": action, **extra}
        if label:
            match = re.search(rf'\[(\d+)\] [\w:]+ "{re.escape(label)}"', prompt)
            assert match, f"element {label!r} not in prompt:\n{prompt}"
            data["element_id"] = int(match.group(1))
        return LLMReply("", "fake", "m", False, Decision.model_validate(data))


def test_agent_explores_spa_safely(server: str) -> None:
    llm = LabelPickingLLM(
        [
            ("type", "Search books", {"text": "dune", "submit": True}),
            ("click", "Next page", {}),
            ("click", "Open Dune 20", {}),
            ("click", "About us", {}),  # target=_blank: must stay in this tab
            ("finish", "", {"goal_covered": True}),
        ]
    )
    recorder = TrafficRecorder("127.0.0.1")
    with BrowserSession(server, recorder) as browser:
        result = Explorer(
            browser, llm, lambda: recorder.captured, "search books and open one",
            server, "127.0.0.1", max_steps=10,
        ).run()  # fmt: skip

    assert result.status == "goal-covered", result
    labels = {e.label for e in result.endpoints}
    assert labels == {"GET /api/search", "GET /api/books/{id}", "GET /api/team"}
    search = next(e for e in result.endpoints if e.label == "GET /api/search")
    assert {p.name for p in search.query_params} == {"q", "page"}
    assert search.sample_count == 2

    # Unsafe elements were never offered to the LLM, and nothing destructive was sent.
    # (Page text may mention them; only the clickable element list matters.)
    first = llm.prompts[0]
    elements = first.split("Elements:", 1)[1]
    for unsafe in ("Delete account", "Password", "Partner site"):
        assert unsafe not in elements
    assert "3 unsafe or off-site elements hidden" in first
    assert "POST /api/account/delete" not in Handler.hits
    # The new-tab link was followed in the same tab.
    assert "GET /about" in Handler.hits
    assert all(s.result.startswith(("1 new", "2 new", "finished")) for s in result.steps)

    # Stage 5: probe the captured search endpoint over real HTTP.
    replayer = Replayer("127.0.0.1", RateLimiter(10_000), RobotsPolicy.allow_all("127.0.0.1"))
    keys = endpoint_keys(recorder.captured)
    samples = [r for k, r in zip(keys, recorder.raw, strict=False) if k == search.key and r]
    report = Prober(replayer).probe(search, samples)
    replayer.close()
    assert report.replayable
    assert report.pagination is not None and report.pagination["confirmed"]
    assert report.auth_type == "none"
