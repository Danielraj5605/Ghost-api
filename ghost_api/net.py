"""Polite networking for requests the tool sends itself: rate limiting and robots.txt."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from ghost_api.config import TOOL_NAME, tool_user_agent
from ghost_api.netguard import (
    NetworkPolicy,
    PolicyDenied,
    ResponseTooLarge,
    guarded_client,
    read_limited,
)

MAX_ROBOTS_BYTES = 512 * 1024


class RateLimiter:
    """Blocks so that calls to `wait()` happen at most `rate` times per second (NFR-18)."""

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be greater than 0")
        self.interval = 1.0 / rate
        self._last = float("-inf")

    def slow_down_to(self, min_interval: float) -> None:
        self.interval = max(self.interval, min_interval)

    def wait(self) -> None:
        # Sleep until the clock itself shows the interval has passed: on Windows with
        # Python < 3.13, monotonic() ticks every ~16 ms and sleep() can end just short.
        target = self._last + self.interval
        while (delay := target - time.monotonic()) > 0:
            time.sleep(delay)
        self._last = time.monotonic()


@dataclass(frozen=True)
class RobotsVerdict:
    allowed: bool
    reason: str
    crawl_delay: float | None = None


def robots_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


class RobotsPolicy:
    """robots.txt rules for one host, reusable for every URL the agent visits (NFR-17)."""

    def __init__(
        self,
        host: str,
        parser: RobotFileParser | None,
        default_allowed: bool,
        reason: str,
        agent: str = TOOL_NAME,
    ) -> None:
        self.host = host
        self._parser = parser
        self._default = default_allowed
        self.reason = reason
        self.agent = agent

    @classmethod
    def allow_all(cls, host: str, reason: str = "robots.txt check skipped") -> RobotsPolicy:
        return cls(host, None, True, reason)

    @classmethod
    def from_text(cls, host: str, robots_txt: str, agent: str = TOOL_NAME) -> RobotsPolicy:
        parser = RobotFileParser()
        parser.parse(robots_txt.splitlines())
        return cls(host, parser, True, "robots.txt rules", agent)

    @property
    def crawl_delay(self) -> float | None:
        if self._parser is None:
            return None
        delay = self._parser.crawl_delay(self.agent)
        return float(delay) if delay is not None else None

    def allows(self, url: str) -> bool:
        """Rules apply to the host they came from; other hosts are not checked here."""
        if (urlsplit(url).hostname or "").lower() != self.host:
            return True
        if self._parser is None:
            return self._default
        return self._parser.can_fetch(self.agent, url)

    def verdict(self, url: str) -> RobotsVerdict:
        allowed = self.allows(url)
        if self._parser is None:
            reason = self.reason
        else:
            reason = "allowed by robots.txt" if allowed else "disallowed by robots.txt"
        return RobotsVerdict(allowed, reason, self.crawl_delay)


class MultiHostRobotsPolicy(RobotsPolicy):
    """Lazily fetches and caches robots.txt per host, so subdomains are checked too (Issue 3).

    The agent may navigate from ``www.example.com`` to ``api.example.com``.  A plain
    ``RobotsPolicy`` only knows the rules for one host and returns ``True`` for any other.
    This wrapper fetches and caches each host's robots.txt on first encounter.

    It exposes the same ``allows(url)`` interface as ``RobotsPolicy`` so it's a drop-in
    replacement wherever the orchestrator or pipeline calls ``robots.allows(url)``.
    """

    def __init__(
        self,
        seed: RobotsPolicy,
        limiter: RateLimiter | None = None,
        fetcher: Callable[[str, RateLimiter | None], RobotsPolicy] | None = None,
    ) -> None:
        super().__init__(seed.host, None, True, seed.reason, seed.agent)
        self._cache: dict[str, RobotsPolicy] = {seed.host: seed}
        self._limiter = limiter
        self._fetch = fetcher or fetch_robots

    def _for(self, url: str) -> RobotsPolicy:
        host = (urlsplit(url).hostname or "").lower()
        if host not in self._cache:
            self._cache[host] = self._fetch(url, self._limiter)
        return self._cache[host]

    @property
    def crawl_delay(self) -> float | None:
        return self._cache[self.host].crawl_delay

    def allows(self, url: str) -> bool:
        """Check the correct host's robots.txt, fetching it lazily if needed."""
        return self._for(url).allows(url)

    def verdict(self, url: str) -> RobotsVerdict:
        return self._for(url).verdict(url)


class DenyPaths(RobotsPolicy):
    """robots.txt plus the project's own rules (ghost-api.toml deny_paths) for one host:
    a denied path is treated exactly like a robots.txt Disallow, everywhere."""

    def __init__(self, inner: RobotsPolicy, deny: tuple[str, ...]) -> None:
        super().__init__(inner.host, None, True, inner.reason, inner.agent)
        self.inner = inner
        self.deny = deny

    @property
    def crawl_delay(self) -> float | None:
        return self.inner.crawl_delay

    def _denied(self, url: str) -> bool:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != self.host:
            return False
        path = parts.path or "/"
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.deny)

    def allows(self, url: str) -> bool:
        return not self._denied(url) and self.inner.allows(url)

    def verdict(self, url: str) -> RobotsVerdict:
        if self._denied(url):
            return RobotsVerdict(False, "denied by the project rules (ghost-api.toml)")
        return self.inner.verdict(url)


def evaluate_robots(robots_txt: str, url: str, agent: str = TOOL_NAME) -> RobotsVerdict:
    """Apply robots.txt rules to a URL. Pure function, so it's easy to test."""
    host = (urlsplit(url).hostname or "").lower()
    return RobotsPolicy.from_text(host, robots_txt, agent).verdict(url)


def fetch_robots(
    url: str,
    limiter: RateLimiter | None = None,
    policy: NetworkPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
) -> RobotsPolicy:
    """Fetch robots.txt, following RFC 9309 for missing or unreachable files."""
    host = (urlsplit(url).hostname or "").lower()
    if limiter:
        limiter.wait()
    # Redirects are followed (RFC 9309), but each hop is checked: a public site's
    # robots.txt must not lead Ghost API to a private address.
    client = guarded_client(
        policy or NetworkPolicy.for_target(url),
        follow_redirects=True,
        timeout=10,
        transport=transport,
    )
    try:
        with (
            client,
            client.stream(
                "GET", robots_url(url), headers={"User-Agent": tool_user_agent()}
            ) as resp,
        ):
            text = read_limited(resp, MAX_ROBOTS_BYTES, MAX_ROBOTS_BYTES).text
    except PolicyDenied as exc:
        return RobotsPolicy(host, None, False, f"robots.txt {exc}")
    except ResponseTooLarge:
        # RFC 9309: at least 500 KiB must be parsed; a bigger file is treated as unreadable.
        return RobotsPolicy(host, None, False, "robots.txt is too large to read")
    except httpx.HTTPError as exc:
        # Unreachable robots.txt: assume everything is disallowed.
        return RobotsPolicy(host, None, False, f"robots.txt unreachable ({type(exc).__name__})")
    if resp.status_code in (401, 403):
        # The site refused to show its rules (Wikipedia does this to clients that don't
        # identify themselves). Not knowing the rules is not permission.
        return RobotsPolicy(
            host,
            None,
            False,
            f"robots.txt is forbidden (HTTP {resp.status_code}), so the site's rules can't be "
            "read. Set GHOST_API_CONTACT in .env so the site can identify you",
        )
    if 400 <= resp.status_code < 500:
        return RobotsPolicy(host, None, True, f"no robots.txt (HTTP {resp.status_code})")
    if resp.status_code >= 500:
        return RobotsPolicy(host, None, False, f"robots.txt server error (HTTP {resp.status_code})")
    return RobotsPolicy.from_text(host, text)


def check_robots(url: str, limiter: RateLimiter | None = None) -> RobotsVerdict:
    return fetch_robots(url, limiter).verdict(url)
