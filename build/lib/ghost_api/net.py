"""Polite networking for requests the tool sends itself: rate limiting and robots.txt."""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from ghost_api.config import TOOL_NAME, USER_AGENT_SUFFIX


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
        now = time.monotonic()
        delay = self._last + self.interval - now
        if delay > 0:
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


def evaluate_robots(robots_txt: str, url: str, agent: str = TOOL_NAME) -> RobotsVerdict:
    """Apply robots.txt rules to a URL. Pure function, so it's easy to test."""
    host = (urlsplit(url).hostname or "").lower()
    return RobotsPolicy.from_text(host, robots_txt, agent).verdict(url)


def fetch_robots(url: str, limiter: RateLimiter | None = None) -> RobotsPolicy:
    """Fetch robots.txt, following RFC 9309 for missing or unreachable files."""
    host = (urlsplit(url).hostname or "").lower()
    if limiter:
        limiter.wait()
    try:
        resp = httpx.get(
            robots_url(url),
            headers={"User-Agent": USER_AGENT_SUFFIX},
            follow_redirects=True,
            timeout=10,
        )
    except httpx.HTTPError as exc:
        # Unreachable robots.txt: assume everything is disallowed.
        return RobotsPolicy(host, None, False, f"robots.txt unreachable ({type(exc).__name__})")
    if 400 <= resp.status_code < 500:
        return RobotsPolicy(host, None, True, f"no robots.txt (HTTP {resp.status_code})")
    if resp.status_code >= 500:
        return RobotsPolicy(host, None, False, f"robots.txt server error (HTTP {resp.status_code})")
    return RobotsPolicy.from_text(host, resp.text)


def check_robots(url: str, limiter: RateLimiter | None = None) -> RobotsVerdict:
    return fetch_robots(url, limiter).verdict(url)
