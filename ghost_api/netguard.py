"""Where Ghost API may connect: never to this computer or the local network by accident.

A public website can redirect, or make its page call, an address such as 127.0.0.1,
192.168.1.1 or the cloud metadata address 169.254.169.254. Ghost API runs on the user's
machine, so following that would let any website reach the user's private network (SSRF).

`NetworkPolicy` decides per destination. Private addresses are blocked unless the user's
own target is local (the demo shop, tests, a site on the LAN): then the run is in *local
target mode* and private addresses are allowed, because the user asked for them.

The check resolves the host name and looks at every address it resolves to. The browser's
traffic goes through `browser/egress.py`, which connects to the exact address that was
checked, so a host name can't be switched to a private address between check and use.
Direct requests (httpx) are checked on every redirect hop; httpx resolves the name again
itself, a small window that is documented in SECURITY.md.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]

MAX_RESPONSE_BYTES = 5_000_000  # a reply bigger than this (after decompression) is not read
MAX_COMPRESSED_BYTES = 2_000_000  # bytes on the wire for one reply


class PolicyDenied(Exception):
    """A destination Ghost API refuses to connect to."""


class ResponseTooLarge(Exception):
    """A reply was bigger than the limit; reading stopped (response-too-large)."""


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def address_problem(ip: IPAddress) -> str | None:
    """Why an address is not on the public internet, or None when it is."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "this computer (loopback)"
    if ip.is_link_local:
        return "a link-local address (e.g. cloud metadata)"
    if ip.is_unspecified:
        return "an unspecified address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_private or not ip.is_global:
        return "a private-network address"
    return None


def _literal_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


@dataclass
class NetworkPolicy:
    """`allow_private`: local target mode. `blocked`: tests replace the address rule."""

    allow_private: bool = False
    resolver: Resolver = system_resolver
    blocked: Callable[[IPAddress, int], str | None] = field(
        default=lambda ip, _port: address_problem(ip)
    )
    _cache: dict[tuple[str, int], list[str]] = field(default_factory=dict, repr=False)

    @classmethod
    def for_target(cls, url: str, resolver: Resolver = system_resolver) -> NetworkPolicy:
        """Local target mode when the user's own target is on this machine or the LAN."""
        host = (urlsplit(url).hostname or "").lower()
        local = host == "localhost" or host.endswith(".localhost")
        if not local:
            try:
                addresses = [_literal_ip(host)] if _literal_ip(host) else [
                    ipaddress.ip_address(a) for a in resolver(host, 443)
                ]  # fmt: skip
            except (OSError, ValueError):
                addresses = []
            local = any(a is not None and address_problem(a) for a in addresses)
        return cls(allow_private=local, resolver=resolver)

    def resolve(self, host: str, port: int) -> list[str]:
        """The addresses `host` may be reached at; raises PolicyDenied if any is blocked.

        All addresses must pass: a name that resolves to one public and one private
        address could be steered to the private one."""
        host = host.strip("[]").lower()
        literal = _literal_ip(host)
        if literal is not None:
            addresses = [str(literal)]
        else:
            key = (host, port)
            if key not in self._cache:
                try:
                    self._cache[key] = self.resolver(host, port)
                except OSError as exc:
                    raise PolicyDenied(f"{host} could not be resolved ({exc})") from exc
            addresses = self._cache[key]
        if not addresses:
            raise PolicyDenied(f"{host} has no address")
        if not self.allow_private:
            for address in addresses:
                problem = self.blocked(ipaddress.ip_address(address), port)
                if problem:
                    raise PolicyDenied(
                        f"blocked {host}: it points to {problem}. Ghost API does not let a "
                        "website reach your computer or local network"
                    )
        return addresses

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PolicyDenied(f"blocked {url[:80]}: only http(s) addresses are allowed")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        self.resolve(parts.hostname, port)


class PinnedBackend(httpcore.SyncBackend):
    """Connects to the address the policy checked, not to a second DNS answer, so a host
    name can't be switched to a private address between check and connection (DNS
    rebinding). TLS still uses the host name, so certificates are checked as usual."""

    def __init__(self, policy: NetworkPolicy) -> None:
        self.policy = policy

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        address = self.policy.resolve(host, port)[0]  # raises PolicyDenied when blocked
        return super().connect_tcp(address, port, timeout, local_address, socket_options)


def guarded_client(policy: NetworkPolicy, **kwargs: Any) -> httpx.Client:
    """An httpx client that checks every request it sends, redirect hops included, and
    connects only to the addresses it checked."""

    def check(request: httpx.Request) -> None:
        policy.check_url(str(request.url))

    if "transport" not in kwargs or kwargs["transport"] is None:
        kwargs.pop("transport", None)
        transport = httpx.HTTPTransport()
        pool = getattr(transport, "_pool", None)
        if pool is not None and hasattr(pool, "_network_backend"):
            pool._network_backend = PinnedBackend(policy)
        kwargs["transport"] = transport
    return httpx.Client(event_hooks={"request": [check]}, **kwargs)


@dataclass
class LimitedBody:
    content: bytes
    encoding: str | None

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


def read_limited(
    response: httpx.Response,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_compressed: int = MAX_COMPRESSED_BYTES,
) -> LimitedBody:
    """Read a streamed reply, stopping as soon as it is too big (on the wire or unpacked),
    so a huge or highly compressed reply never fills memory."""
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_compressed:
        raise ResponseTooLarge(f"response-too-large: {int(declared):,} bytes declared")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLarge(f"response-too-large: over {max_bytes:,} bytes unpacked")
        if response.num_bytes_downloaded > max_compressed:
            raise ResponseTooLarge(f"response-too-large: over {max_compressed:,} bytes")
        chunks.append(chunk)
    return LimitedBody(b"".join(chunks), response.encoding)
