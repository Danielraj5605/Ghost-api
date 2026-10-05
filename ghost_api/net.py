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


def evaluate_robots(robots_txt: str, url: str, agent: str = TOOL_NAME) -> RobotsVerdict:
    """Apply robots.txt rules to a URL. Pure function, so it's easy to test."""
    parser = RobotFileParser()
    parser.parse(robots_txt.splitlines())
    allowed = parser.can_fetch(agent, url)
    delay = parser.crawl_delay(agent)
    reason = "allowed by robots.txt" if allowed else "disallowed by robots.txt"
    return RobotsVerdict(allowed, reason, float(delay) if delay is not None else None)


def check_robots(url: str, limiter: RateLimiter | None = None) -> RobotsVerdict:
    """Fetch and apply robots.txt, following RFC 9309 for missing or unreachable files."""
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
        return RobotsVerdict(False, f"robots.txt unreachable ({type(exc).__name__})")
    if 400 <= resp.status_code < 500:
        return RobotsVerdict(True, f"no robots.txt (HTTP {resp.status_code})")
    if resp.status_code >= 500:
        return RobotsVerdict(False, f"robots.txt server error (HTTP {resp.status_code})")
    return evaluate_robots(resp.text, url)
