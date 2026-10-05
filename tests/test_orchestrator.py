"""Agent loop tests with a fake browser and a scripted fake LLM."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from ghost_api.agent.orchestrator import NO_PROGRESS_STEPS, Explorer
from ghost_api.agent.prompts import step_prompt
from ghost_api.agent.tools import Decision, is_typeable
from ghost_api.browser.driver import ActionError
from ghost_api.browser.snapshot import Element, PageSnapshot
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.llm.gateway import BudgetExceeded, LLMReply, LLMUnavailable
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.probe.replay import Replayer

HOST = "ex.com"
START = "https://ex.com/"


def captured(url: str) -> CapturedRequest:
    return CapturedRequest("GET", url, 200, "fetch", {}, None, None, "application/json", '{"a": 1}')


class FakeBrowser:
    """Each element id maps to the API URL that clicking it 'triggers'."""

    def __init__(self, triggers: dict[int, str] | None = None) -> None:
        self.triggers = triggers or {}
        self.traffic: list[CapturedRequest] = []
        self.actions: list[tuple[Any, ...]] = []
        self.popup_urls: list[str] = []
        self._url = START
        self.next_url_after_click: str | None = None

    @property
    def url(self) -> str:
        return self._url

    def _emit(self, url: str | None) -> list[CapturedRequest]:
        if not url:
            return []
        req = captured(url)
        self.traffic.append(req)
        return [req]

    def snapshot(self, target_host: str, robots: RobotsPolicy | None = None) -> PageSnapshot:
        els = [Element(i, "button", f"Button {i}", in_viewport=True) for i in self.triggers]
        els.append(Element(99, "input:search", "Search", in_viewport=True))
        return PageSnapshot(self._url, "Page", "top", "text", els)

    def goto(self, url: str) -> list[CapturedRequest]:
        self.actions.append(("goto", url))
        self._url = url
        return self._emit("https://ex.com/api/home" if url == START else None)

    def click(self, element_id: int) -> list[CapturedRequest]:
        self.actions.append(("click", element_id))
        if self.next_url_after_click:
            self._url, self.next_url_after_click = self.next_url_after_click, None
        return self._emit(self.triggers.get(element_id))

    def type_text(self, element_id: int, text: str, submit: bool) -> list[CapturedRequest]:
        self.actions.append(("type", element_id, text, submit))
        return self._emit(f"https://ex.com/api/search?q={text}")

    def scroll(self, direction: str = "down") -> list[CapturedRequest]:
        self.actions.append(("scroll", direction))
        return []

    def go_back(self) -> list[CapturedRequest]:
        self.actions.append(("back",))
        self._url = START
        return []


class ScriptedLLM:
    def __init__(self, *decisions: dict[str, Any] | BaseException) -> None:
        self.script = list(decisions)
        self.prompts: list[str] = []

    def ask(self, messages: list[dict[str, str]], response_model: Any = None,
            temperature: float = 0.0) -> LLMReply[Decision]:  # fmt: skip
        self.prompts.append(messages[-1]["content"])
        item = self.script.pop(0) if self.script else {"thought": "idle", "action": "scroll"}
        if isinstance(item, BaseException):
            raise item
        return LLMReply("", "fake", "m", False, Decision.model_validate(item))


def explorer(browser: FakeBrowser, llm: ScriptedLLM, **kw: Any) -> Explorer:
    return Explorer(browser, llm, lambda: browser.traffic, "find items", START, HOST, **kw)


def act(action: str, **kw: Any) -> dict[str, Any]:
    return {"thought": f"do {action}", "action": action, **kw}


def test_explores_then_finishes_when_goal_covered() -> None:
    browser = FakeBrowser({1: "https://ex.com/api/items/1"})
    llm = ScriptedLLM(
        act("type", element_id=99, text="dune", submit=True),
        act("click", element_id=1),
        act("finish", goal_covered=True),
    )
    seen: list[str] = []
    result = explorer(browser, llm, on_step=lambda r: seen.append(r.action)).run()
    assert result.status == "goal-covered"
    assert seen == ["type", "click", "finish"]
    assert {e.label for e in result.endpoints} == {
        "GET /api/home", "GET /api/search", "GET /api/items/{id}",
    }  # fmt: skip
    assert result.steps[0].new_endpoints == ["GET /api/search"]
    assert "NEW ENDPOINT" in result.steps[1].result
    # The second prompt shows the first action's outcome and the endpoints so far.
    assert "GET /api/search" in llm.prompts[1] and "type(" in llm.prompts[1]


def test_finish_without_goal_covered() -> None:
    result = explorer(FakeBrowser(), ScriptedLLM(act("finish"))).run()
    assert result.status == "finished"


def test_max_steps() -> None:
    result = explorer(FakeBrowser(), ScriptedLLM(), max_steps=3).run()
    assert result.status == "max-steps" and len(result.steps) == 3


def test_stops_when_nothing_new_is_found() -> None:
    result = explorer(FakeBrowser(), ScriptedLLM(), max_steps=50).run()
    assert result.status == "no-progress"
    assert len(result.steps) == NO_PROGRESS_STEPS


def test_time_limit() -> None:
    times = iter([0.0, 0.0, 61.0])
    result = explorer(FakeBrowser(), ScriptedLLM(), max_minutes=1, clock=lambda: next(times)).run()
    assert result.status == "time-limit"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (BudgetExceeded("used up"), "llm-budget"),
        (LLMUnavailable({"gemini": "503"}), "llm-unavailable"),
    ],
)
def test_llm_failures_stop_gracefully(error: Exception, status: str) -> None:
    browser = FakeBrowser()
    result = explorer(browser, ScriptedLLM(error)).run()
    assert result.status == status
    assert [e.label for e in result.endpoints] == ["GET /api/home"]  # partial results kept


def test_unknown_element_and_non_text_field_are_rejected() -> None:
    browser = FakeBrowser({1: "https://ex.com/api/a"})
    llm = ScriptedLLM(
        act("click", element_id=42),
        act("type", element_id=1, text="x"),
        act("finish"),
    )
    result = explorer(browser, llm).run()
    assert result.steps[0].result.startswith("rejected: [42] is not in the element list")
    assert result.steps[1].result.startswith("rejected: [1] is a button")
    assert ("click", 42) not in browser.actions


@pytest.mark.parametrize(
    "url", ["https://evil.com/x", "mailto:a@ex.com", "https://ex.com/private/data"]
)
def test_navigation_is_restricted(url: str) -> None:
    robots = RobotsPolicy.from_text(HOST, "User-agent: *\nDisallow: /private/\n")
    browser = FakeBrowser()
    result = explorer(browser, ScriptedLLM(act("navigate", url=url), act("finish")),
                      robots=robots).run()  # fmt: skip
    assert result.steps[0].result.startswith("rejected")
    assert ("goto", url) not in browser.actions


def test_relative_navigation_is_resolved() -> None:
    browser = FakeBrowser()
    explorer(browser, ScriptedLLM(act("navigate", url="/books?page=2"), act("finish"))).run()
    assert ("goto", "https://ex.com/books?page=2") in browser.actions


def test_leaving_the_site_goes_back() -> None:
    browser = FakeBrowser({1: None})  # type: ignore[dict-item]
    browser.next_url_after_click = "https://other.com/landing"
    result = explorer(browser, ScriptedLLM(act("click", element_id=1), act("finish"))).run()
    assert ("back",) in browser.actions
    assert "left the site, went back" in result.steps[0].result


def test_popups_are_followed_in_the_same_tab_if_allowed() -> None:
    browser = FakeBrowser({1: None, 2: None})  # type: ignore[dict-item]
    original_click: Callable[[int], list[CapturedRequest]] = browser.click

    def click(element_id: int) -> list[CapturedRequest]:
        browser.popup_urls.append(
            "https://ex.com/detail/7" if element_id == 1 else "https://ads.com/x"
        )
        return original_click(element_id)

    browser.click = click  # type: ignore[method-assign]
    llm = ScriptedLLM(act("click", element_id=1), act("click", element_id=2), act("finish"))
    explorer(browser, llm).run()
    assert ("goto", "https://ex.com/detail/7") in browser.actions
    assert ("goto", "https://ads.com/x") not in browser.actions


def test_action_errors_are_reported_not_raised() -> None:
    browser = FakeBrowser({1: "https://ex.com/api/a"})

    def broken(element_id: int) -> list[CapturedRequest]:
        raise ActionError("element [1] is no longer on the page")

    browser.click = broken  # type: ignore[method-assign]
    result = explorer(browser, ScriptedLLM(act("click", element_id=1), act("finish"))).run()
    assert result.steps[0].result == "failed: element [1] is no longer on the page"


def test_interrupt_keeps_partial_results() -> None:
    result = explorer(FakeBrowser(), ScriptedLLM(KeyboardInterrupt())).run()
    assert result.status == "interrupted"
    assert result.endpoints


@pytest.mark.parametrize(
    "bad",
    [
        {"thought": "t", "action": "click"},
        {"thought": "t", "action": "type", "element_id": 1},
        {"thought": "t", "action": "navigate"},
        {"thought": "t", "action": "type", "element_id": 1, "text": "x" * 201},
        {"thought": "t", "action": "jump"},
    ],
)
def test_decision_validation(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Decision.model_validate(bad)


def test_decision_args_only_include_relevant_fields() -> None:
    d = Decision.model_validate(act("type", element_id=3, text="dune", submit=True, url="x"))
    assert d.args() == {"element_id": 3, "text": "dune", "submit": True}


def test_is_typeable() -> None:
    assert is_typeable("input:search") and is_typeable("textarea") and is_typeable("searchbox")
    assert not is_typeable("input:checkbox") and not is_typeable("link")


def test_prompt_contains_the_essentials() -> None:
    link = Element(3, "link", "Next", "https://ex.com/p?page=2", True)
    snap = PageSnapshot("https://ex.com/p?x=1", "T", "top", "hello", [link], blocked=2)
    text = step_prompt("list books", 2, 40, snap, [], HOST, [])
    for needle in ("GOAL: list books", "STEP 2 of at most 40", '[3] link "Next" -> /p?page=2',
                   "2 unsafe or off-site elements hidden", "(none yet)"):  # fmt: skip
        assert needle in text


# --- Stage 5: call_endpoint, ask_user, bot protection ---


def test_bot_protection_stops_before_asking_the_llm() -> None:
    browser = FakeBrowser()
    original = browser.snapshot

    def challenge(target_host: str, robots: RobotsPolicy | None = None) -> PageSnapshot:
        snap = original(target_host, robots)
        snap.title, snap.text = "Just a moment...", "Checking your browser"
        return snap

    browser.snapshot = challenge  # type: ignore[method-assign]
    llm = ScriptedLLM()
    result = explorer(browser, llm).run()
    assert result.status == "bot-protection"
    assert llm.prompts == []


def test_ask_user() -> None:
    questions: list[str] = []
    llm = ScriptedLLM(act("ask_user", question="Please log in"), act("finish"))
    result = explorer(FakeBrowser(), llm, ask_user=lambda q: questions.append(q) or "done").run()
    assert questions == ["Please log in"]
    assert result.steps[0].result == "user answered: done"


def test_ask_user_without_a_human() -> None:
    llm = ScriptedLLM(act("ask_user", question="Log in?"), act("finish"))
    result = explorer(FakeBrowser(), llm).run()
    assert result.steps[0].result.startswith("rejected: no human is available")


def _replayer(seen: list[httpx.Request]) -> Replayer:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [{"id": 1}, {"id": 2}]})

    return Replayer(
        HOST,
        RateLimiter(10_000),
        RobotsPolicy.allow_all(HOST),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_call_endpoint_replays_with_changed_params() -> None:
    browser = FakeBrowser()
    raws: list[RawRequest | None] = []
    browser_goto = browser.goto

    def goto(url: str) -> list[CapturedRequest]:
        out = browser_goto(url)
        raws.extend(RawRequest("GET", r.url + "?page=1", {"accept": "*/*"}) for r in out)
        return out

    browser.goto = goto  # type: ignore[method-assign]
    seen: list[httpx.Request] = []
    added: list[CapturedRequest] = []

    def add_sample(c: CapturedRequest, r: RawRequest) -> None:
        added.append(c)
        browser.traffic.append(c)
        raws.append(r)

    llm = ScriptedLLM(
        act("call_endpoint", endpoint="GET /api/home", params={"page": "2"}),
        act("call_endpoint", endpoint="GET /api/nope"),
        act("finish"),
    )
    result = explorer(
        browser, llm, replayer=_replayer(seen), raw_requests=lambda: raws, add_sample=add_sample
    ).run()
    assert result.steps[0].result == "called GET /api/home with {'page': '2'}: HTTP 200, 2 records"
    assert "page=2" in str(seen[0].url)
    assert len(added) == 1
    assert result.steps[1].result.startswith("rejected: no endpoint named 'GET /api/nope'")


def test_call_endpoint_needs_a_replayer() -> None:
    llm = ScriptedLLM(act("call_endpoint", endpoint="GET /api/home"), act("finish"))
    result = explorer(FakeBrowser(), llm).run()
    assert result.steps[0].result == "rejected: calling endpoints directly is not enabled"
