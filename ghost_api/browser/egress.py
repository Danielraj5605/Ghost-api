"""A tiny local proxy that every browser connection goes through (the network boundary).

Playwright's request interception does not see redirect hops: when a page or an API call
is redirected, the browser follows it on its own. A proxy sees every connection the
browser opens — redirects, images, scripts, websockets — so each destination is checked
with `NetworkPolicy` before anything is sent to it. The proxy then connects to the very
address that passed the check (no second DNS lookup to switch to a private address).

HTTPS goes through `CONNECT host:port` tunnels: the proxy sees the host, not the path or
the content (paths and robots.txt are checked by the route gate in `gate.py`). Plain HTTP
requests are forwarded one per connection.
"""

from __future__ import annotations

import contextlib
import select
import socket
import socketserver
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

from ghost_api.netguard import NetworkPolicy, PolicyDenied

HEAD_LIMIT = 64 * 1024
CONNECT_TIMEOUT_S = 15
IDLE_TIMEOUT_S = 120


class EgressProxy:
    def __init__(
        self, policy: NetworkPolicy, on_block: Callable[[str], None] | None = None
    ) -> None:
        self.policy = policy
        self.on_block = on_block
        self.blocked: list[str] = []
        self._lock = threading.Lock()
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> EgressProxy:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> EgressProxy:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- one client connection ---

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(CONNECT_TIMEOUT_S)
        try:
            head = _read_head(client)
        except OSError:
            return
        if not head:
            return
        line, _, rest = head.partition(b"\r\n")
        try:
            method, target, version = line.decode("latin-1").split(" ", 2)
        except ValueError:
            _reply(client, 400, "Bad Request")
            return
        if method.upper() == "CONNECT":
            host, _, port_text = target.rpartition(":")
            port = int(port_text) if port_text.isdigit() else 443
            upstream = self._connect(client, host, port)
            if upstream is None:
                return
            _reply(client, 200, "Connection Established")
            _pipe(client, upstream)
            return
        parts = urlsplit(target)
        if parts.scheme != "http" or not parts.hostname:
            _reply(client, 400, "Bad Request")
            return
        upstream = self._connect(client, parts.hostname, parts.port or 80)
        if upstream is None:
            return
        path = parts.path or "/"
        if parts.query:
            path += f"?{parts.query}"
        headers, _, body = rest.partition(b"\r\n\r\n")
        kept = [
            h
            for h in headers.split(b"\r\n")
            if h and not h.lower().startswith((b"proxy-connection:", b"connection:"))
        ]
        request = f"{method} {path} {version}\r\n".encode("latin-1")
        request += b"\r\n".join([*kept, b"Connection: close"]) + b"\r\n\r\n" + body
        upstream.sendall(request)
        _pipe(client, upstream)

    def _connect(self, client: socket.socket, host: str, port: int) -> socket.socket | None:
        try:
            addresses = self.policy.resolve(host, port)
        except PolicyDenied as exc:
            self._block(str(exc))
            _reply(client, 403, "Blocked by Ghost API")
            return None
        for address in addresses:
            try:
                return socket.create_connection((address, port), timeout=CONNECT_TIMEOUT_S)
            except OSError:
                continue
        _reply(client, 502, "Bad Gateway")
        return None

    def _block(self, message: str) -> None:
        with self._lock:
            self.blocked.append(message)
        if self.on_block is not None:
            with contextlib.suppress(Exception):
                self.on_block(message)


def _read_head(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < HEAD_LIMIT:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _reply(client: socket.socket, status: int, reason: str) -> None:
    """Answer the browser; any answer but "200 Connection Established" ends the connection."""
    with contextlib.suppress(OSError):
        if status == 200:
            client.sendall(f"HTTP/1.1 200 {reason}\r\n\r\n".encode("latin-1"))
            return
        client.sendall(
            f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            .encode("latin-1")
        )  # fmt: skip
        client.close()


def _pipe(a: socket.socket, b: socket.socket) -> None:
    """Copy bytes both ways until either side closes or the connection idles out."""
    sockets = [a, b]
    try:
        for s in sockets:
            s.settimeout(None)
        while True:
            readable, _, broken = select.select(sockets, [], sockets, IDLE_TIMEOUT_S)
            if broken or not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (b if source is a else a).sendall(data)
    except OSError:
        return
    finally:
        for s in sockets:
            with contextlib.suppress(OSError):
                s.close()
