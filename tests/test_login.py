"""Phase 4c: saved logins (`ghost-api login`), reused by runs and direct calls."""

import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ghost_api.browser import auth_state
from ghost_api.cli import app

runner = CliRunner()
SESSION = "sess-" + "4f2a9c"  # built at run time, not a real session


class Site:
    """A site whose API needs the session cookie set by /login."""

    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                logged_in = f"session={SESSION}" in (self.headers.get("Cookie") or "")
                if self.path == "/login":
                    self._send(200, "text/html", b"<p>welcome</p>",
                               {"Set-Cookie": f"session={SESSION}; Path=/; HttpOnly"})  # fmt: skip
                elif self.path == "/":
                    page = b"<script>fetch('/api/me?v=1').then(r => r.text())</script>home"
                    self._send(200, "text/html", page)
                elif self.path.startswith("/api/me"):
                    body = {"user": {"id": 1, "plan": "pro"}} if logged_in else {"error": "login"}
                    self._send(200 if logged_in else 401, "application/json",
                               json.dumps(body).encode())  # fmt: skip
                else:
                    self._send(404, "text/plain", b"")

            def _send(self, status: int, ctype: str, body: bytes,
                      extra: dict[str, str] | None = None) -> None:  # fmt: skip
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def site() -> Iterator[Site]:
    s = Site()
    yield s
    s.httpd.shutdown()


def state_for(url: str) -> dict[str, Any]:
    return {"cookies": [{"name": "session", "value": SESSION, "domain": "127.0.0.1", "path": "/",
                         "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"}],
            "origins": []}  # fmt: skip


def test_saved_logins_are_protected_and_listed(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    meta = auth_state.save(db, "ex.com", state_for("https://ex.com"))
    data = next(auth_state.logins_dir(db).glob("*.session")).read_bytes()
    assert SESSION.encode() not in data or sys.platform != "win32"  # encrypted on Windows
    assert meta["encrypted"] == (sys.platform == "win32")
    state, _ = auth_state.load(db, "ex.com") or ({}, {})
    assert state["cookies"][0]["value"] == SESSION
    listed = json.loads(runner.invoke(app, ["--json", "login", "--list", "--db", str(db)]).stdout)
    assert listed["logins"][0]["site"] == "ex.com" and "value" not in json.dumps(listed)


def test_cookie_header_follows_browser_rules() -> None:
    state = {"cookies": [
        {"name": "a", "value": "1", "domain": ".ex.com", "path": "/", "secure": True},
        {"name": "b", "value": "2", "domain": "api.ex.com", "path": "/v2", "expires": -1},
        {"name": "old", "value": "3", "domain": "ex.com", "path": "/", "expires": 10},
    ]}  # fmt: skip
    assert auth_state.cookie_header(state, "https://api.ex.com/v2/x") == "a=1; b=2"
    assert auth_state.cookie_header(state, "http://ex.com/") == ""  # secure; expired
    assert auth_state.cookie_header(state, "https://other.com/") == ""


def test_runs_and_fetch_use_the_saved_login(site: Site, tmp_path: Path) -> None:
    db, out = tmp_path / "g.db", tmp_path / "out"
    auth_state.save(db, "127.0.0.1", state_for(site.url))
    run = runner.invoke(app, ["--json", "run", site.url + "/", "-g", "my account", "--no-agent",
                              "--no-verify", "--db", str(db), "--out", str(out)])  # fmt: skip
    assert run.exit_code == 0, run.output
    me = next(e for e in json.loads(run.stdout)["endpoints"] if e["path"] == "/api/me")
    assert me["statuses"] == [200]  # the browser was logged in

    fetched = runner.invoke(app, ["--json", "fetch", "127.0.0.1", "get_me", "--db", str(db),
                                  "--out", str(out)])  # fmt: skip
    assert fetched.exit_code == 0, fetched.output
    assert json.loads(fetched.stdout)["records"]  # the saved cookie went with the direct call

    with_db = ["--db", str(db)]
    assert "Deleted" in runner.invoke(app, ["logout", "127.0.0.1", *with_db]).output
    failed = runner.invoke(app, ["fetch", "127.0.0.1", "get_me", *with_db, "--out", str(out)])
    assert failed.exit_code != 0  # logged out: the cookie in the stored sample was redacted


def test_login_command_saves_what_the_browser_holds(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ghost_api.browser import driver

    opened: list[Any] = []
    real_init = driver.BrowserSession.__init__

    def headless(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["headed"] = False  # tests have no screen
        real_init(self, *args, **kwargs)
        opened.append(self)

    def user_logs_in(*_args: Any, **_kwargs: Any) -> str:
        opened[0].page.goto(site.url + "/login")  # what the user does in the window
        return ""

    monkeypatch.setattr(driver.BrowserSession, "__init__", headless)
    monkeypatch.setattr("typer.prompt", user_logs_in)
    db = tmp_path / "g.db"
    result = runner.invoke(app, ["login", site.url + "/", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "Saved the login for 127.0.0.1" in result.output and SESSION not in result.output
    state, _ = auth_state.load(db, "127.0.0.1") or ({}, {})
    assert auth_state.cookie_header(state, site.url + "/api/me") == f"session={SESSION}"
