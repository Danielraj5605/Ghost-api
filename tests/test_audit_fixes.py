"""Regression tests for the issues in ghost_api_issues_and_fixes.md."""

import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ghost_api import __version__
from ghost_api.analysis.analyzer import analyze
from ghost_api.capture.models import CapturedRequest
from ghost_api.cli import app, safe_output
from ghost_api.config import USER_AGENT_SUFFIX, Settings, check_language
from ghost_api.errors import friendly_error
from ghost_api.events import EventBus, Notice
from ghost_api.llm.gateway import LLMGateway, LLMUnavailable
from ghost_api.llm.providers import Provider
from ghost_api.memory.store import Store, StoreError
from ghost_api.net import MultiHostRobotsPolicy, RateLimiter, RobotsPolicy
from ghost_api.ui import terminal
from ghost_api.ui.repl import _find_url
from tests.fake_secrets import fake_value
from tests.test_orchestrator import FakeBrowser, ScriptedLLM, act, explorer

runner = CliRunner()


# 1. Rate limits are remembered between runs.


def test_provider_cooldowns_survive_a_new_gateway(tmp_path: Path) -> None:
    provider = Provider("alpha", "https://alpha.test/v1", "m1", api_key=fake_value("alpha"))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(429, headers={"retry-after": "600"})

    def gateway() -> LLMGateway:
        return LLMGateway(
            [provider],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _s: None,
            cooldown_file=tmp_path / "cooldowns.json",
        )

    with pytest.raises(LLMUnavailable):
        gateway().ask([{"role": "user", "content": "hi"}])
    first = len(calls)
    with pytest.raises(LLMUnavailable, match="cooling down"):
        gateway().ask([{"role": "user", "content": "hi"}])
    assert len(calls) == first  # the next run did not ask the rate-limited provider again


def test_a_damaged_cooldown_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "cooldowns.json").write_text("not json", encoding="utf-8")
    LLMGateway([], cooldown_file=tmp_path / "cooldowns.json")


# 2. Endpoints from earlier runs are kept.


def cap(url: str) -> CapturedRequest:
    return CapturedRequest("GET", url, 200, "fetch", {}, None, None, "application/json", '{"a": 1}')


def test_endpoints_from_earlier_runs_are_kept_until_a_refresh_finds_them_gone(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "g.db") as store:
        first = store.create_run("ex.com", "https://ex.com/", "authors")
        store.save_endpoints("ex.com", first, analyze([cap("https://ex.com/api/authors")]))
        second = store.create_run("ex.com", "https://ex.com/", "quotes")
        store.save_endpoints("ex.com", second, analyze([cap("https://ex.com/api/quotes")]))
        labels = {e.label for e in store.current_endpoints("ex.com")}
        assert labels == {"GET /api/authors", "GET /api/quotes"}

        third = store.create_run("ex.com", "https://ex.com/", "refresh: quotes")
        store.save_endpoints("ex.com", third, analyze([cap("https://ex.com/api/quotes")]))
        authors = [e.key for e in store.load_endpoints("ex.com") if "authors" in e.key]
        store.mark_gone("ex.com", authors, third)
        assert {e.label for e in store.current_endpoints("ex.com")} == {"GET /api/quotes"}

        # Seen again later: it is back.
        fourth = store.create_run("ex.com", "https://ex.com/", "authors")
        store.save_endpoints("ex.com", fourth, analyze([cap("https://ex.com/api/authors")]))
        assert len(store.current_endpoints("ex.com")) == 2


def test_an_early_literal_template_is_replaced_by_a_better_one(tmp_path: Path) -> None:
    with Store(tmp_path / "g.db") as store:
        first = store.create_run("ex.com", "https://ex.com/", "one quote")
        store.save_endpoints("ex.com", first, analyze([cap("https://ex.com/api/quotes/7")]))
        second = store.create_run("ex.com", "https://ex.com/", "quotes")
        urls = [f"https://ex.com/api/quotes/{i}" for i in (1, 2, 3, 4)]
        store.save_endpoints("ex.com", second, analyze([cap(u) for u in urls]))
        assert [e.label for e in store.current_endpoints("ex.com")] == ["GET /api/quotes/{id}"]


# 3. robots.txt is checked for every host the browser reaches.


def test_robots_rules_are_fetched_for_each_new_host() -> None:
    fetched: list[str] = []

    def fetcher(url: str, _limiter: RateLimiter | None) -> RobotsPolicy:
        fetched.append(url)
        return RobotsPolicy.from_text("api.ex.com", "User-agent: *\nDisallow: /private")

    seed = RobotsPolicy.from_text("ex.com", "User-agent: *\nDisallow:")
    robots = MultiHostRobotsPolicy(seed, None, fetcher)
    assert robots.allows("https://ex.com/anything")
    assert not robots.allows("https://api.ex.com/private/x")
    assert robots.allows("https://api.ex.com/public")
    assert fetched == ["https://api.ex.com/private/x"]  # once per host
    assert isinstance(robots, RobotsPolicy)  # usable wherever one host's rules were


# 4. --version


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and __version__ in result.output


# 5. "Goal covered" needs nothing missing and at least one endpoint.


def test_goal_is_not_covered_while_the_agent_says_something_is_missing() -> None:
    llm = ScriptedLLM(act("finish", goal_covered=True, missing="the author pages"))
    assert explorer(FakeBrowser(), llm).run().status == "finished"


# 6. User-agent


def test_user_agent_names_the_project() -> None:
    assert "github.com/Danielraj5605/Ghost-api" in USER_AGENT_SUFFIX


# 8. A damaged database


def test_a_damaged_database_gives_a_plain_message(tmp_path: Path) -> None:
    db = tmp_path / "broken.db"
    db.write_bytes(b"this is not sqlite" * 200)
    with pytest.raises(StoreError, match="damaged or is not a Ghost API database"):
        Store(db)
    result = runner.invoke(app, ["inspect", "--db", str(db)])
    assert isinstance(result.exception, StoreError)  # `entry()` prints it without a traceback


# 9. No stdout at all (pythonw)


def test_safe_output_accepts_no_stream() -> None:
    safe_output(None)


# 10. One language check


def test_language_is_checked_in_one_place() -> None:
    assert check_language("python") == "python"
    with pytest.raises(ValueError, match="Supported"):
        check_language("cobol")
    with pytest.raises(ValueError, match="Supported"):
        Settings(url="https://ex.com/", goal="x", lang="cobol")


# 11 + 17. Errors


def test_an_llm_timeout_is_not_blamed_on_the_website() -> None:
    message = friendly_error(httpx.ReadTimeout("The read operation timed out"))
    assert "website" not in message


def test_the_root_cause_is_kept() -> None:
    try:
        try:
            raise OSError("disk full")
        except OSError as inner:
            raise RuntimeError("could not save the report") from inner
    except RuntimeError as exc:
        message = friendly_error(exc)
    assert "could not save the report" in message and "disk full" in message


def test_a_known_cause_is_explained() -> None:
    try:
        try:
            raise OSError("net::ERR_NAME_NOT_RESOLVED")
        except OSError as inner:
            raise RuntimeError("visit failed") from inner
    except RuntimeError as exc:
        assert "address could not be found" in friendly_error(exc)


# 12. watch start with nothing to watch


def test_watch_start_without_watches_stops_at_once(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    Store(db).close()
    started = time.monotonic()
    result = runner.invoke(app, ["watch", "start", "--db", str(db)])
    assert result.exit_code == 1 and "No watches yet" in result.output
    assert time.monotonic() - started < 10


# 14. A failing listener is reported once


def test_a_failing_listener_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()

    def broken(_event: Any) -> None:
        raise RuntimeError("display broke")

    bus.subscribe(broken)
    with caplog.at_level(logging.WARNING, logger="ghost_api"):
        bus.emit(Notice("a"))
        bus.emit(Notice("b"))
    assert len(bus.errors) == 2
    assert [r.message for r in caplog.records].count(
        "A display or listener failed (the run continues): RuntimeError: display broke"
    ) == 1


# 16. Emails are not websites


@pytest.mark.parametrize(
    ("line", "url"),
    [
        ("ask john.doe@example.com about quotes", None),
        ("quotes.toscrape.com list quotes", "quotes.toscrape.com"),
        ("write to a@b.com, then open example.org/x", "example.org/x"),
    ],
)
def test_find_url_skips_email_addresses(line: str, url: str | None) -> None:
    assert _find_url(line) == url


# 22. Links stay usable in terminals without hyperlink support


def test_file_links_show_the_full_url_without_hyperlink_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("WT_SESSION", "TERM_PROGRAM", "GHOST_API_SHORT_LINKS"):
        monkeypatch.delenv(name, raising=False)
    report = tmp_path / "report.html"
    assert terminal.file_link(report).plain == report.resolve().as_uri()
    monkeypatch.setenv("WT_SESSION", "1")
    monkeypatch.chdir(tmp_path)
    assert terminal.file_link(report).plain == "report.html"


# 24. inspect summary


def test_inspect_counts_endpoints_across_sites(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    with Store(db) as store:
        for site in ("a.com", "b.com"):
            run = store.create_run(site, f"https://{site}/", "x")
            store.save_endpoints(site, run, analyze([cap(f"https://{site}/api/items")]))
    result = runner.invoke(app, ["inspect", "--db", str(db)])
    assert "2 site(s) explored, 2 endpoint(s) across 2 run(s)" in result.output


# 27. Stop message


def test_stop_message_says_how_far_the_run_got() -> None:
    terminal.PROGRESS.update(site="ex.com", step=6, endpoints=3)
    try:
        message = terminal.stop_message()
    finally:
        terminal.PROGRESS.update(site="", step=0, endpoints=0)
    assert message == (
        "Stopped on ex.com after step 6. Found 3 endpoints so far; what was captured is saved."
    )
