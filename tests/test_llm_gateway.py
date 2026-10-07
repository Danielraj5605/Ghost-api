"""Gateway tests use a fake HTTP transport: no network, no quota used."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from ghost_api.llm.gateway import BudgetExceeded, LLMGateway, LLMUnavailable, extract_json
from ghost_api.llm.providers import Provider, default_providers
from ghost_api.memory.store import Store
from tests.fake_secrets import bearer, fake_jwt, fake_token

A = Provider("alpha", "https://alpha.test/v1", "a-model", api_key="fake-removed-a")
B = Provider("beta", "https://beta.test/v1", "b-model", api_key="fake-removed-b", json_mode=False)


class Answer(BaseModel):
    action: str
    element_id: int


def ok(content: str, tokens: int = 10) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": tokens, "completion_tokens": 1},
        },
    )


Handler = Callable[[httpx.Request], httpx.Response]


class Fake:
    """Routes requests per provider host to a queue of responses; records requests."""

    def __init__(self, **queues: list[httpx.Response]) -> None:
        self.queues = queues
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host.split(".")[0]
        return self.queues[host].pop(0)

    def hosts(self) -> list[str]:
        return [r.url.host.split(".")[0] for r in self.requests]


def gateway(fake: Fake, providers: list[Provider] | None = None, **kw: Any) -> LLMGateway:
    sleeps: list[float] = []
    gw = LLMGateway(
        providers or [A, B],
        client=httpx.Client(transport=httpx.MockTransport(fake)),
        sleep=sleeps.append,
        **kw,
    )
    gw.sleeps = sleeps  # type: ignore[attr-defined]
    return gw


ASK = [{"role": "user", "content": "click something"}]
GOOD = '{"action": "click", "element_id": 3}'


def test_plain_text_reply_and_usage() -> None:
    fake = Fake(alpha=[ok("hello", tokens=7)])
    gw = gateway(fake)
    reply = gw.ask(ASK)
    assert (reply.text, reply.provider, reply.cached) == ("hello", "alpha", False)
    assert gw.usage.calls == 1 and gw.usage.tokens == 8
    sent = fake.requests[0]
    assert sent.headers["authorization"] == "Bearer fake-removed-a"
    assert json.loads(sent.content)["model"] == "a-model"


def test_structured_reply_is_validated() -> None:
    fake = Fake(alpha=[ok(GOOD)])
    reply = gateway(fake).ask(ASK, response_model=Answer)
    assert reply.parsed == Answer(action="click", element_id=3)
    body = json.loads(fake.requests[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert "JSON schema" in body["messages"][0]["content"]


def test_overloaded_provider_is_skipped_at_once_when_another_is_available() -> None:
    fake = Fake(alpha=[httpx.Response(503)], beta=[ok(GOOD), ok(GOOD)])
    gw = gateway(fake)
    assert gw.ask(ASK, response_model=Answer).provider == "beta"
    assert fake.hosts() == ["alpha", "beta"]  # no retries on the overloaded one
    assert gw.sleeps == []  # type: ignore[attr-defined]
    # beta has no JSON mode, so no response_format is sent to it.
    assert "response_format" not in json.loads(fake.requests[-1].content)
    gw.ask([{"role": "user", "content": "next"}], response_model=Answer)
    assert fake.hosts() == ["alpha", "beta", "beta"]  # alpha rests for a while


def test_last_provider_is_retried_with_backoff() -> None:
    fake = Fake(alpha=[httpx.Response(503), httpx.Response(502), ok(GOOD)])
    gw = gateway(fake, providers=[A])
    assert gw.ask(ASK, response_model=Answer).provider == "alpha"
    assert gw.sleeps == [2.0, 4.0]  # type: ignore[attr-defined]


def test_retry_after_header_is_honored_and_capped() -> None:
    fake = Fake(
        alpha=[
            httpx.Response(503, headers={"retry-after": "7"}),
            httpx.Response(503, headers={"retry-after": "999"}),
            ok("x"),
        ],
    )
    gw = gateway(fake, providers=[A])
    gw.ask(ASK)
    assert gw.sleeps == [7.0, 20.0]  # type: ignore[attr-defined]


def test_quota_error_cools_the_provider_down() -> None:
    now = [0.0]
    gemini_429 = [
        {"error": {"code": 429, "message": "quota exceeded", "details": [{"retryDelay": "37s"}]}}
    ]
    fake = Fake(
        alpha=[httpx.Response(429, json=gemini_429), ok("alpha back")], beta=[ok("b1"), ok("b2")]
    )
    gw = gateway(fake, clock=lambda: now[0])
    assert gw.ask(ASK).provider == "beta"  # no retries on the same provider
    assert fake.hosts() == ["alpha", "beta"]
    assert gw.sleeps == []  # type: ignore[attr-defined]
    now[0] = 30.0  # still cooling down: alpha is not even tried
    assert gw.ask([{"role": "user", "content": "2"}]).provider == "beta"
    assert fake.hosts() == ["alpha", "beta", "beta"]
    now[0] = 38.0  # cooldown over
    assert gw.ask([{"role": "user", "content": "3"}]).provider == "alpha"
    assert gw.usage.calls == 4 and gw.usage.answered == 3


def test_budget_counts_answered_calls_only() -> None:
    fake = Fake(alpha=[httpx.Response(429)], beta=[ok("x")])
    gw = gateway(fake, max_calls=1)
    gw.ask(ASK)  # one rate-limited attempt + one answer: within a budget of 1
    assert gw.usage.answered == 1


def test_auth_error_skips_provider_without_retrying() -> None:
    fake = Fake(
        alpha=[httpx.Response(401, json={"error": {"message": "API key not valid"}})],
        beta=[ok("fine")],
    )
    gw = gateway(fake)
    assert gw.ask(ASK).provider == "beta"
    assert fake.hosts() == ["alpha", "beta"]
    assert gw.sleeps == []  # type: ignore[attr-defined]


def test_invalid_json_is_repaired() -> None:
    fake = Fake(
        alpha=[ok("Sure! I would click."), ok('```json\n{"action": "click"}\n```'), ok(GOOD)]
    )
    reply = gateway(fake).ask(ASK, response_model=Answer)
    assert reply.parsed == Answer(action="click", element_id=3)
    last = json.loads(fake.requests[-1].content)["messages"]
    assert last[-1]["role"] == "user" and "not valid" in last[-1]["content"]
    assert "element_id" in last[-1]["content"]  # the validation error is explained


def test_persistently_invalid_json_falls_back() -> None:
    fake = Fake(alpha=[ok("nope")] * 3, beta=[ok(GOOD)])
    reply = gateway(fake).ask(ASK, response_model=Answer)
    assert reply.provider == "beta"


def test_all_providers_fail() -> None:
    fake = Fake(alpha=[httpx.Response(500)] * 3, beta=[httpx.Response(400, text="bad model")])
    with pytest.raises(LLMUnavailable) as err:
        gateway(fake).ask(ASK)
    assert set(err.value.errors) == {"alpha/a-model", "beta/b-model"}
    assert "HTTP 400" in err.value.errors["beta/b-model"]


def test_network_error_is_retried() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return ok("recovered")

    gw = LLMGateway(
        [A], client=httpx.Client(transport=httpx.MockTransport(flaky)), sleep=lambda s: None
    )
    assert gw.ask(ASK).text == "recovered"


def test_providers_without_keys_are_skipped() -> None:
    no_key = Provider("alpha", "https://alpha.test/v1", "m", api_key=None)
    fake = Fake(beta=[ok("from beta")])
    assert gateway(fake, providers=[no_key, B]).ask(ASK).provider == "beta"
    with pytest.raises(LLMUnavailable):
        LLMGateway([no_key]).ask(ASK)


def test_cache_serves_repeat_prompts(tmp_path: Path) -> None:
    with Store(tmp_path / "t.db") as store:
        fake = Fake(alpha=[ok(GOOD)])
        gw = gateway(fake, store=store)
        first = gw.ask(ASK, response_model=Answer)
        second = gw.ask(ASK, response_model=Answer)
        assert not first.cached and second.cached
        assert second.parsed == first.parsed
        assert len(fake.requests) == 1
        assert gw.usage.calls == 1 and gw.usage.cache_hits == 1


def test_cache_is_per_model_and_temperature(tmp_path: Path) -> None:
    with Store(tmp_path / "t.db") as store:
        fake = Fake(alpha=[ok("one"), ok("two")])
        gw = gateway(fake, store=store)
        gw.ask(ASK)
        assert gw.ask(ASK, temperature=0.7).text == "two"


def test_budget_is_enforced() -> None:
    fake = Fake(alpha=[ok("a"), ok("b")])
    gw = gateway(fake, max_calls=1)
    gw.ask(ASK)
    with pytest.raises(BudgetExceeded):
        gw.ask([{"role": "user", "content": "another"}])


def test_secrets_are_redacted_before_sending() -> None:
    jwt = fake_jwt()
    fake = Fake(alpha=[ok("x")])
    gateway(fake).ask([{"role": "user", "content": f"header was {bearer()}, token {jwt}"}])
    sent = fake.requests[0].content.decode()
    assert jwt not in sent and fake_token() not in sent


@pytest.mark.parametrize(
    "text",
    [GOOD, f"```json\n{GOOD}\n```", f"Here you go: {GOOD} hope that helps", f"```\n{GOOD}\n```"],
)
def test_extract_json(text: str) -> None:
    assert extract_json(text) == {"action": "click", "element_id": 3}


def test_default_providers_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GHOST_API_GROQ_MODEL", "custom-model")
    monkeypatch.setenv("GHOST_API_PROVIDERS", "groq,gemini")
    providers = default_providers()
    assert [p.name for p in providers] == ["groq", "gemini"]
    assert providers[0].model == "custom-model"
    assert providers[1].api_key == "g"
    assert "api_key" not in repr(providers[1])


def test_unknown_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHOST_API_PROVIDERS", "gemini,skynet")
    with pytest.raises(ValueError, match="skynet"):
        default_providers()


def test_error_messages_are_reported_for_list_and_dict_bodies() -> None:
    gemini_style = [{"error": {"code": 503, "message": "high demand"}}]
    fake = Fake(
        alpha=[httpx.Response(503, json=gemini_style, headers={"retry-after": "3600"})],
        beta=[httpx.Response(404, json={"error": {"message": "model not found"}})],
    )
    with pytest.raises(LLMUnavailable) as err:
        gateway(fake).ask(ASK)
    assert "high demand" in err.value.errors["alpha/a-model"]
    assert "model not found" in err.value.errors["beta/b-model"]


def test_waits_once_when_every_provider_is_briefly_rate_limited() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    fake = Fake(
        alpha=[httpx.Response(429, headers={"retry-after": "20"}), ok("alpha after wait")],
        beta=[httpx.Response(429, headers={"retry-after": "300"})],
    )
    gw = LLMGateway(
        [A, B],
        client=httpx.Client(transport=httpx.MockTransport(fake)),
        sleep=sleep,
        clock=lambda: now[0],
    )
    assert gw.ask(ASK).text == "alpha after wait"
    assert sleeps == [21.0]


def test_does_not_wait_for_long_cooldowns() -> None:
    fake = Fake(
        alpha=[httpx.Response(429, headers={"retry-after": "3600"})],
        beta=[httpx.Response(429, headers={"retry-after": "7200"})],
    )
    gw = gateway(fake)
    with pytest.raises(LLMUnavailable):
        gw.ask(ASK)
    assert gw.sleeps == []  # type: ignore[attr-defined]


def test_default_order_includes_the_lite_gemini_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GHOST_API_PROVIDERS", raising=False)
    monkeypatch.setenv("GHOST_API_GEMINI_LITE_MODEL", "lite-x")
    providers = {p.name: p for p in default_providers()}
    assert list(providers) == ["gemini", "groq", "gemini-lite", "openrouter"]
    assert providers["gemini-lite"].model == "lite-x"


# --- model-level routing (several candidate models per provider) ---

MULTI = Provider("alpha", "https://alpha.test/v1", "a1", api_key="K", fallback_models=("a2", "a3"))


def model_of(request: httpx.Request) -> str:
    return str(json.loads(request.content)["model"])


def routed(responses: dict[str, list[httpx.Response]]) -> tuple[LLMGateway, list[str]]:
    """Fake API answering per model name; returns the gateway and the models it called."""
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = model_of(request) if request.url.host.startswith("alpha") else "beta"
        called.append(model)
        return responses[model].pop(0)

    gw = LLMGateway(
        [MULTI, B],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )
    return gw, called


def test_retired_model_moves_to_the_next_model_and_stays_dead() -> None:
    gw, called = routed(
        {
            "a1": [httpx.Response(404, json={"error": {"message": "retired"}})],
            "a2": [ok("from a2"), ok("again a2")],
        }
    )
    first = gw.ask(ASK)
    assert (first.provider, first.model, first.text) == ("alpha", "a2", "from a2")
    gw.ask([{"role": "user", "content": "second"}])
    assert called == ["a1", "a2", "a2"]  # a1 is not tried again


def test_rate_limited_model_uses_another_model_of_the_same_provider() -> None:
    gw, called = routed(
        {"a1": [httpx.Response(429, headers={"retry-after": "30"})], "a2": [ok("a2 answers")]}
    )
    assert gw.ask(ASK).model == "a2"
    assert called == ["a1", "a2"]


def test_bad_key_disables_every_model_of_the_provider() -> None:
    gw, called = routed(
        {"a1": [httpx.Response(401, json={"error": "bad key"})], "beta": [ok("beta"), ok("beta 2")]}
    )
    assert gw.ask(ASK).provider == "beta"
    gw.ask([{"role": "user", "content": "second"}])
    assert called == ["a1", "beta", "beta"]  # a2 and a3 never tried


def test_waits_when_the_only_living_route_is_briefly_rate_limited() -> None:
    """The evaluation bug: dead (404) routes must not stop the gateway from waiting."""
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("beta"):
            return httpx.Response(404, json={"error": {"message": "model unavailable"}})
        if now[0] < 5:
            return httpx.Response(429, headers={"retry-after": "8"})
        return ok("after the wait")

    single = Provider("alpha", "https://alpha.test/v1", "a1", api_key="K")
    gw = LLMGateway(
        [single, B],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleep,
        clock=lambda: now[0],
    )
    assert gw.ask(ASK).text == "after the wait"
    assert sleeps == [9.0]


def test_default_providers_have_fallback_models() -> None:
    for p in default_providers():
        assert p.fallback_models, p.name
