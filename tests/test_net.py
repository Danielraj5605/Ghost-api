import time

import httpx
import pytest

from ghost_api.config import tool_user_agent
from ghost_api.net import RateLimiter, RobotsPolicy, evaluate_robots, fetch_robots, robots_url

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


# --- robots.txt that the site refuses to show (found with Wikipedia) ---


def _fake_get(status: int, text: str = "", seen: list[dict[str, str]] | None = None):  # type: ignore[no-untyped-def]
    def get(url: str, headers: dict[str, str], **kwargs: object) -> httpx.Response:
        if seen is not None:
            seen.append(headers)
        return httpx.Response(status, text=text, request=httpx.Request("GET", url))

    return get


@pytest.mark.parametrize("status", [401, 403])
def test_forbidden_robots_txt_means_not_allowed(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(httpx, "get", _fake_get(status))
    policy = fetch_robots("https://wiki.test/w/rest.php/v1/search/title?q=x")
    assert not policy.allows("https://wiki.test/anything")
    assert "forbidden" in policy.reason and "GHOST_API_CONTACT" in policy.reason


@pytest.mark.parametrize("status", [404, 410])
def test_missing_robots_txt_still_means_allowed(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(httpx, "get", _fake_get(status))
    assert fetch_robots("https://site.test/").allows("https://site.test/api/x")


def test_the_disallow_rule_is_applied_once_it_can_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = "User-agent: *\nDisallow: /w/\nAllow: /w/load.php?\n"
    monkeypatch.setattr(httpx, "get", _fake_get(200, rules))
    policy = fetch_robots("https://wiki.test/")
    assert not policy.allows("https://wiki.test/w/rest.php/v1/search/title?q=x")
    assert policy.allows("https://wiki.test/wiki/Article")


def test_contact_is_added_only_when_the_user_sets_it(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(httpx, "get", _fake_get(404, seen=seen))
    monkeypatch.delenv("GHOST_API_CONTACT", raising=False)
    fetch_robots("https://site.test/")
    monkeypatch.setenv("GHOST_API_CONTACT", "me@example.org")
    fetch_robots("https://site.test/")
    assert "contact" not in seen[0]["User-Agent"]
    assert seen[1]["User-Agent"].endswith("(contact: me@example.org)")
    assert tool_user_agent().startswith("GhostAPI/")
