"""End-to-end: a real browser loads a local page that leaks secrets; none may be captured."""

import json
import threading
from collections.abc import Iterator
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ghost_api.browser.driver import BrowserSession
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.memory.store import Store

PAGE = b"""<!doctype html><html><body>
<img src="/logo.png">
<script>
  fetch('/api/items?page=1&api_key=fake-removed',
        {headers: {'Authorization': 'Bearer fake-token-removed'}});
  fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
                       body: JSON.stringify({user: 'bob', password: 'fake-removed'})});
  fetch('/collect?event=view');
  fetch('/api/page.html');
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, ctype: str, body: bytes, cookie: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if cookie:
            self.send_header("Set-Cookie", "sessionid=fake-removed; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, "text/html", PAGE, cookie=True)
        elif self.path.startswith("/api/items"):
            body = {
                "items": [{"id": 1, "author": "Ada", "key": "OL1W"}],
                "accessToken": "fake-removed",
            }
            self._send(200, "application/json", json.dumps(body).encode())
        elif self.path.startswith("/api/page.html"):
            self._send(200, "text/html", b"<p>not json</p>")
        elif self.path.startswith("/collect"):
            self._send(200, "application/json", b"{}")
        else:
            self._send(404, "text/plain", b"nope")

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(200, "application/json", b'{"ok": true}')


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def test_capture_filters_noise_and_redacts_secrets(server: str, tmp_path: Path) -> None:
    recorder = TrafficRecorder("127.0.0.1")
    with BrowserSession(server, recorder) as browser:
        browser.goto(server)
        browser.goto(server)  # second load sends the cookie set by the first

    paths = sorted(
        r.url.split("127.0.0.1")[1].split("?")[0].split("/", 1)[1] for r in recorder.captured
    )
    assert paths == ["api/items", "api/items", "api/login", "api/login"]
    assert recorder.dropped["analytics"] >= 1  # /collect
    assert recorder.dropped["non-json"] >= 1  # /api/page.html

    with Store(tmp_path / "t.db") as store:
        run_id = store.create_run("127.0.0.1", server, "test")
        for req in recorder.captured:
            store.add_request(run_id, req)
        rows = store.requests_for_run(run_id)
        stored = json.dumps([asdict(r) for r in rows])

    assert "fake-removed" not in stored
    # Normal data survives redaction.
    items = next(json.loads(r.resp_body or "") for r in rows if "/api/items" in r.url)
    assert items["items"] == [{"id": 1, "author": "Ada", "key": "OL1W"}]
    assert items["accessToken"] == "[REDACTED]"
    assert "page=1" in stored
