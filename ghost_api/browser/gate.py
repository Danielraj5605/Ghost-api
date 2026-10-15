"""Rules for requests the browser makes, applied before they are sent.

  - Page loads (navigations) anywhere must be allowed by that host's robots.txt.
  - The site's own API calls (fetch / XHR to the site or its subdomains) must be allowed
    by robots.txt and are spaced out per host by the run's rate limit, like Ghost API's
    direct requests. The page's own images, scripts and styles load as in any browser.
  - Third-party calls (analytics, maps, CDNs...) are not checked against robots.txt: the
    page makes them, not Ghost API, and fetching every third party's robots.txt would add
    traffic. They still go through the egress proxy (no private addresses).

Every decision to block is reported once per host and path through `on_block`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from ghost_api.capture.filters import is_third_party
from ghost_api.net import RateLimiter, RobotsPolicy

API_TYPES = {"fetch", "xhr", "eventsource"}
MAX_WAIT_S = 10.0


class _Frame(Protocol):
    @property
    def parent_frame(self) -> object | None: ...


class _Request(Protocol):
    @property
    def url(self) -> str: ...

    @property
    def resource_type(self) -> str: ...

    @property
    def frame(self) -> _Frame: ...

    def is_navigation_request(self) -> bool: ...


class _Route(Protocol):
    @property
    def request(self) -> _Request: ...

    def continue_(self) -> None: ...

    def abort(self, error_code: str | None = None) -> None: ...


class RequestGate:
    def __init__(
        self,
        target_host: str,
        robots: RobotsPolicy | None,
        limiter: RateLimiter | None,
        on_block: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.target_host = target_host
        self.robots = robots
        self.limiter = limiter
        self.on_block = on_block
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._reported: set[str] = set()
        self.blocked = 0
        self.delayed = 0

    def decide(self, url: str, resource_type: str, navigation: bool) -> str | None:
        """Why the request must not be sent, or None (after any rate-limit wait)."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return None  # data:, blob: ... never reach the network
        first_party = not is_third_party(url, self.target_host)
        api_call = first_party and resource_type in API_TYPES
        if (navigation or api_call) and self.robots is not None and not self.robots.allows(url):
            return "disallowed by robots.txt"
        if api_call or (navigation and first_party):
            self._space_out((parts.hostname or "").lower())
        return None

    def handle(self, route: _Route) -> None:
        request = route.request
        try:  # only the main page's own loads, not embedded frames (ads, videos...)
            navigation = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:  # noqa: BLE001 - a service worker or detached frame
            navigation = False
        reason = self.decide(request.url, request.resource_type, navigation)
        if reason is None:
            route.continue_()
            return
        self.blocked += 1
        self._report(request.url, reason)
        route.abort("blockedbyclient")

    def _space_out(self, host: str) -> None:
        if self.limiter is None:
            return
        now = self._clock()
        wait = min(self._last.get(host, float("-inf")) + self.limiter.interval - now, MAX_WAIT_S)
        if wait > 0:
            self.delayed += 1
            self._sleep(wait)
        self._last[host] = self._clock()

    def _report(self, url: str, reason: str) -> None:
        parts = urlsplit(url)
        key = f"{parts.hostname}{parts.path}"
        if key in self._reported or self.on_block is None:
            return
        self._reported.add(key)
        self.on_block(f"Blocked {parts.hostname}{parts.path[:60]}: {reason}")
