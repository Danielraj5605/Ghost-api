import time

import pytest

from ghost_api.net import RateLimiter, RobotsPolicy, evaluate_robots, robots_url

ROBOTS = """
User-agent: *
Disallow: /private/
Crawl-delay: 2

User-agent: GhostAPI
Disallow: /no-ghosts/
"""


def test_robots_url() -> None:
    assert robots_url("https://example.com/a/b?c=1") == "https://example.com/robots.txt"


def test_robots_specific_agent_rules_win() -> None:
    assert evaluate_robots(ROBOTS, "https://example.com/no-ghosts/x").allowed is False
    # The GhostAPI group replaces the * group, so /private/ is allowed for us.
    assert evaluate_robots(ROBOTS, "https://example.com/private/x").allowed is True


def test_robots_generic_rules() -> None:
    robots = "User-agent: *\nDisallow: /api/\nCrawl-delay: 3\n"
    verdict = evaluate_robots(robots, "https://example.com/api/books")
    assert verdict.allowed is False
    assert verdict.crawl_delay == 3.0
    assert evaluate_robots(robots, "https://example.com/books").allowed is True


def test_empty_robots_allows_everything() -> None:
    assert evaluate_robots("", "https://example.com/anything").allowed is True


def test_rate_limiter_spaces_calls() -> None:
    limiter = RateLimiter(20)  # 50 ms apart
    start = time.monotonic()
    for _ in range(4):
        limiter.wait()
    assert time.monotonic() - start >= 0.14


def test_rate_limiter_rejects_bad_rate() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_robots_policy_checks_every_url_on_its_host() -> None:
    policy = RobotsPolicy.from_text("example.com", "User-agent: *\nDisallow: /admin/\n")
    assert policy.allows("https://example.com/books")
    assert not policy.allows("https://example.com/admin/users")
    assert policy.allows("https://api.example.com/admin/x")  # other host: not covered


def test_robots_policy_defaults() -> None:
    assert RobotsPolicy.allow_all("example.com").allows("https://example.com/anything")
    blocked = RobotsPolicy("example.com", None, False, "unreachable")
    assert not blocked.allows("https://example.com/")
    assert blocked.verdict("https://example.com/").reason == "unreachable"
