"""The network boundary: private addresses, redirects, robots.txt and rate limits for the
browser, and size limits for replies (improvement.md, P1)."""

import gzip
import ipaddress
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from ghost_api.browser.driver import BrowserSession
from ghost_api.browser.egress import EgressProxy
from ghost_api.browser.gate import RequestGate
from ghost_api.capture import recorder as recorder_module
from ghost_api.capture.models import RawRequest
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.netguard import (
    NetworkPolicy,
    PolicyDenied,
    ResponseTooLarge,
    address_problem,
    guarded_client,
    read_limited,
)
from ghost_api.probe.replay import Replayer, ReplayRefused

# --- which addresses are private ------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "fe80::1",
        "fc00::1",
        "0.0.0.0",
        "100.64.0.1",  # carrier-grade NAT
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
    ],
)
def test_private_addresses_are_blocked(address: str) -> None:
    assert address_problem(ipaddress.ip_address(address))
    policy = NetworkPolicy()
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(PolicyDenied):
        policy.check_url(f"http://{host}/")


def test_public_addresses_pass() -> None:
    assert address_problem(ipaddress.ip_address("93.184.216.34")) is None
    assert address_problem(ipaddress.ip_address("2606:4700::1111")) is None


def test_a_name_that_resolves_to_a_private_address_is_blocked() -> None:
    names = {"evil.example": ["93.184.216.34", "10.0.0.5"], "good.example": ["93.184.216.34"]}
    policy = NetworkPolicy(resolver=lambda host, _port: names[host])
    policy.check_url("https://good.example/")
    with pytest.raises(PolicyDenied, match="private-network"):
        policy.check_url("https://evil.example/")


def test_local_targets_switch_on_local_mode() -> None:
    assert NetworkPolicy.for_target("http://127.0.0.1:8000/").allow_private
    assert NetworkPolicy.for_target("http://localhost:3000/").allow_private
    public = NetworkPolicy.for_target("https://x.example/", lambda _h, _p: ["93.184.216.34"])
    assert not public.allow_private
    lan = NetworkPolicy.for_target("http://nas.example/", lambda _h, _p: ["192.168.1.20"])
    assert lan.allow_private


def test_only_http_is_allowed() -> None:
    with pytest.raises(PolicyDenied, match="http"):
        NetworkPolicy(allow_private=True).check_url("file:///etc/passwd")


# --- test servers ---------------------------------------------------------------------------


class Server:
    """A local HTTP server that records every path it is asked for."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.hits: list[tuple[float, str]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                server.hits.append((time.monotonic(), self.path))
                route = routes.get(self.path.split("?")[0])
                if callable(route):
                    route = route()
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, headers, body = route
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def paths(self) -> list[str]:
        return [p for _, p in self.hits]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def victim() -> Iterator[Server]:
    """Stands in for a private-network service (a router page, cloud metadata...)."""
    server = Server({"/secret": (200, {"Content-Type": "application/json"}, b'{"key": 1}')})
    yield server
    server.close()


def html(body: str) -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "text/html"}, f"<html><body>{body}</body></html>".encode()


def json_reply(body: bytes = b'{"ok": true}') -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "application/json"}, body


def treat_as_private(port: int) -> NetworkPolicy:
    """Everything is local in tests, so one server's port plays the private address."""
    return NetworkPolicy(
        allow_private=False,
        blocked=lambda _ip, p: "a private-network address" if p == port else None,
    )


# --- the browser can't be steered to private addresses --------------------------------------


def test_browser_redirects_and_page_calls_never_reach_a_private_address(victim: Server) -> None:
    secret = f"{victim.url}/secret"
    site = Server(
        {
            "/": html(f"<script>fetch('{secret}').catch(() => {{}})</script>home"),
            "/go": (302, {"Location": secret}, b""),
            "/api/hop": (302, {"Location": secret}, b""),
            "/calls": html("<script>fetch('/api/hop').catch(() => {})</script>calls"),
        }
    )
    blocked: list[str] = []
    try:
        recorder = TrafficRecorder("127.0.0.1")
        policy = treat_as_private(victim.port)
        with BrowserSession(site.url + "/", recorder, policy=policy, on_block=blocked.append) as b:
            b.goto(site.url + "/")  # the page calls the private address itself
            b.goto(site.url + "/go")  # a redirect to the private address: refused
            b.goto(site.url + "/calls")  # an API call redirected to the private address
        assert victim.hits == []
        assert "/go" in site.paths() and "/api/hop" in site.paths()
        assert blocked and all("private-network" in m for m in blocked)
        assert recorder.dropped["blocked-network"] >= 1
    finally:
        site.close()


def test_local_target_mode_still_reaches_local_servers() -> None:
    site = Server({"/": html("<script>fetch('/api/items')</script>"), "/api/items": json_reply()})
    try:
        recorder = TrafficRecorder("127.0.0.1")
        with BrowserSession(site.url + "/", recorder) as browser:
            assert browser.policy.allow_private
            browser.goto(site.url + "/")
        assert "/api/items" in site.paths()
    finally:
        site.close()


# --- robots.txt and rate limits for browser traffic -----------------------------------------


def test_browser_api_calls_obey_robots_and_the_rate_limit() -> None:
    calls = "".join(f"fetch('/api/items?i={i}');" for i in range(4))
    site = Server(
        {
            "/": html(f"<script>{calls} fetch('/api/private/users');</script>"),
            "/api/items": json_reply(),
            "/api/private/users": json_reply(),
        }
    )
    blocked: list[str] = []
    try:
        robots = RobotsPolicy.from_text("127.0.0.1", "User-agent: *\nDisallow: /api/private")
        gate = RequestGate("127.0.0.1", robots, RateLimiter(5), on_block=blocked.append)
        recorder = TrafficRecorder("127.0.0.1")
        with BrowserSession(site.url + "/", recorder, gate=gate) as browser:
            browser.goto(site.url + "/")
        assert not any(p.startswith("/api/private") for p in site.paths())
        assert blocked == ["Blocked 127.0.0.1/api/private/users: disallowed by robots.txt"]
        api_times = [t for t, p in site.hits if p.startswith("/api/items")]
        assert len(api_times) == 4
        gaps = [b - a for a, b in zip(api_times, api_times[1:], strict=False)]
        # 5 per second: requests leave 0.2 s apart; arrival times jitter on a busy machine,
        # so check the overall pace rather than the single smallest gap.
        assert sum(gaps) / len(gaps) >= 0.17 and min(gaps) >= 0.05
    finally:
        site.close()


def test_a_disallowed_page_is_not_loaded() -> None:
    site = Server({"/": html("ok"), "/admin": html("admin")})
    try:
        robots = RobotsPolicy.from_text("127.0.0.1", "User-agent: *\nDisallow: /admin")
        gate = RequestGate("127.0.0.1", robots, None)
        with BrowserSession(site.url + "/", TrafficRecorder("127.0.0.1"), gate=gate) as b:
            b.goto(site.url + "/")
            with pytest.raises(Exception):  # noqa: B017
                b.page.goto(site.url + "/admin")
        assert "/admin" not in site.paths()
    finally:
        site.close()


def test_third_party_resources_are_not_robots_checked() -> None:
    robots = RobotsPolicy.from_text("127.0.0.1", "User-agent: *\nDisallow: /")
    gate = RequestGate("ex.com", robots, None)
    assert gate.decide("https://cdn.other.net/lib.js", "script", False) is None
    assert gate.decide("https://ex.com/x", "fetch", False) is None  # rules of another host


# --- direct requests (httpx) ------------------------------------------------------------------


def test_direct_requests_check_every_redirect_hop(victim: Server) -> None:
    site = Server({"/go": (302, {"Location": f"{victim.url}/secret"}, b"")})
    try:
        policy = treat_as_private(victim.port)
        with (
            guarded_client(policy, follow_redirects=True) as client,
            pytest.raises(PolicyDenied),
        ):
            client.get(site.url + "/go")
        assert victim.hits == [] and site.paths() == ["/go"]
    finally:
        site.close()


# --- size limits ------------------------------------------------------------------------------


def test_big_replies_stop_being_read_at_the_limit() -> None:
    big = b"[" + b"1," * 400_000 + b"1]"
    bomb = gzip.compress(b'{"x": "' + b"a" * 3_000_000 + b'"}')  # small on the wire

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/big":
            return httpx.Response(200, content=big)
        return httpx.Response(200, content=bomb, headers={"Content-Encoding": "gzip"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with client.stream("GET", "https://x.example/big") as resp, pytest.raises(ResponseTooLarge):
            read_limited(resp, max_bytes=500_000)
        with client.stream("GET", "https://x.example/bomb") as resp:
            assert len(bomb) < 100_000
            with pytest.raises(ResponseTooLarge, match="unpacked"):
                read_limited(resp, max_bytes=1_000_000)
        with client.stream("GET", "https://x.example/big") as resp:
            assert read_limited(resp).content == big


def test_replays_are_refused_when_redirected_to_a_private_address(victim: Server) -> None:
    site = Server(
        {
            "/api/items": (302, {"Location": f"{victim.url}/secret"}, b""),
            "/api/huge": json_reply(b"[" + b"1," * 3_000_000 + b"1]"),
        }
    )
    try:
        replayer = Replayer(
            "127.0.0.1", RateLimiter(100), RobotsPolicy.allow_all("127.0.0.1"),
            policy=treat_as_private(victim.port),
        )  # fmt: skip
        with pytest.raises(ReplayRefused, match="private-network"):
            replayer.send(RawRequest("GET", f"{victim.url}/secret", {}, None))
        redirected = replayer.send(RawRequest("GET", f"{site.url}/api/items", {}, None))
        assert redirected.status == 302  # replays never follow redirects
        assert victim.hits == []
        result = replayer.send(RawRequest("GET", f"{site.url}/api/huge", {}, None))
        assert result.body is None and result.error and "response-too-large" in result.error
    finally:
        site.close()


def test_the_browser_does_not_store_oversized_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder_module, "MAX_CAPTURE_BYTES", 10_000)
    site = Server(
        {
            "/": html("<script>fetch('/api/big'); fetch('/api/small')</script>"),
            "/api/big": json_reply(b'{"rows": [' + b"1," * 20_000 + b"1]}"),
            "/api/small": json_reply(),
        }
    )
    try:
        recorder = TrafficRecorder("127.0.0.1")
        with BrowserSession(site.url + "/", recorder) as browser:
            browser.goto(site.url + "/")
        assert [c.url.split("/")[-1] for c in recorder.captured] == ["small"]
        assert recorder.dropped["response-too-large"] == 1
    finally:
        site.close()


def test_https_style_tunnels_go_through_the_proxy(victim: Server) -> None:
    """Browsers open HTTPS through CONNECT tunnels: allowed hosts get a working tunnel,
    private ones are refused before any connection is made."""
    site = Server({"/api/items": json_reply()})
    try:
        with EgressProxy(treat_as_private(victim.port)) as proxy:
            port = int(proxy.url.rsplit(":", 1)[1])

            def tunnel(target_port: int) -> bytes:
                with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                    sock.sendall(f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n\r\n".encode())
                    answer = sock.recv(4096)
                    if b" 200 " in answer:
                        sock.sendall(
                            b"GET /api/items HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                        )
                        while chunk := sock.recv(4096):
                            answer += chunk
                    return answer

            assert b'{"ok": true}' in tunnel(site.port)
            assert b" 403 " in tunnel(victim.port)
            assert proxy.blocked
        assert victim.hits == [] and site.paths() == ["/api/items"]
    finally:
        site.close()
