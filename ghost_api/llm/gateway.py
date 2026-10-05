"""One reliable way to call an LLM on free tiers (Stage 3).

All supported providers speak the OpenAI-compatible chat-completions protocol, so a
provider is just a base URL, an API key and a model name (NFR-27).

`ask()` behavior:
  - Redacts secrets from every message before it leaves the machine (NFR-13).
  - Serves repeated prompts from the SQLite cache (FR-44).
  - Retries 429 / 5xx / network errors with backoff, then falls back to the next
    provider (NFR-3, NFR-7).
  - With `response_model`, parses and validates JSON and re-asks up to
    MAX_REPAIRS times when the reply is invalid (NFR-8).
  - Counts calls and tokens and enforces the call budget (NFR-2).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Generic, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ghost_api.capture.filters import redact_text
from ghost_api.config import USER_AGENT_SUFFIX
from ghost_api.llm.providers import Provider
from ghost_api.memory.store import Store

T = TypeVar("T", bound=BaseModel)
Message = dict[str, str]

MAX_RETRIES = 2  # per provider, for 429 / 5xx / network errors
MAX_REPAIRS = 2  # re-asks after an invalid JSON reply
BACKOFF_BASE_S = 2.0
MAX_BACKOFF_S = 20.0
TIMEOUT_S = 90.0
DEFAULT_COOLDOWN_S = 60.0  # skip a rate-limited provider this long
MAX_COOLDOWN_WAIT_S = 65.0  # when all are rate-limited, wait at most this long


class LLMError(Exception):
    """Base class for gateway errors."""


class BudgetExceeded(LLMError):
    pass


class LLMUnavailable(LLMError):
    """Every provider failed. `errors` says why, per provider."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        detail = "; ".join(f"{name}: {err}" for name, err in errors.items()) or "none configured"
        super().__init__(f"No LLM provider could answer ({detail})")


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None, quota: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.quota = quota  # rate/quota limit: retrying right away won't help


class _Fatal(Exception):
    """Provider can't serve this request (bad key, bad model, bad request)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Usage:
    calls: int = 0  # every HTTP request sent
    answered: int = 0  # requests that returned a reply; this is what the budget counts
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hits: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    retries: int = 0  # 429 / 5xx / network errors retried
    repairs: int = 0  # invalid JSON replies sent back for correction
    fallbacks: int = 0  # times a provider was given up on for a request
    problems: Counter[str] = field(default_factory=Counter)  # short reasons, for diagnosis

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMReply(Generic[T]):
    text: str
    provider: str
    model: str
    cached: bool
    parsed: T | None = None


class LLMGateway:
    def __init__(
        self,
        providers: list[Provider],
        store: Store | None = None,
        max_calls: int | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.providers = [p for p in providers if p.api_key]
        # One route per (provider, model): each provider's candidate models, in order.
        self._routes = [
            replace(p, model=m)
            for p in self.providers
            for m in dict.fromkeys((p.model, *p.fallback_models))
        ]
        self._dead: set[str] = set()  # route keys whose model is gone (404)
        self._disabled: set[str] = set()  # provider names with a bad key (401/403)
        self.store = store
        self.max_calls = max_calls
        self.usage = Usage()
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._sleep = sleep
        self._clock = clock
        self._cooldown_until: dict[str, float] = {}  # route key -> clock time

    def close(self) -> None:
        self._client.close()

    # --- public API ---

    def ask(
        self,
        messages: list[Message],
        response_model: type[T] | None = None,
        temperature: float = 0.0,
    ) -> LLMReply[T]:
        if not self.providers:
            raise LLMUnavailable({})
        messages = [{**m, "content": redact_text(m["content"])} for m in messages]
        if response_model is not None:
            messages = _with_json_instructions(messages, response_model)

        for provider in self._routes:
            cached = self._cache_get(provider, messages, temperature)
            if cached is not None:
                parsed = _parse(cached, response_model) if response_model else None
                if response_model is None or parsed is not None:
                    self.usage.cache_hits += 1
                    return LLMReply(cached, provider.name, provider.model, True, parsed)

        try:
            return self._ask_chain(messages, response_model, temperature)
        except LLMUnavailable:
            # Every provider is rate-limited. If one frees up soon, wait for it once.
            wait = self._shortest_cooldown()
            if wait is None or wait > MAX_COOLDOWN_WAIT_S:
                raise
            self.usage.problems[f"waited {wait:.0f}s for a rate limit to clear"] += 1
            self._sleep(wait)
            return self._ask_chain(messages, response_model, temperature)

    def _alive(self) -> list[Provider]:
        return [
            r for r in self._routes if _key(r) not in self._dead and r.name not in self._disabled
        ]

    def _shortest_cooldown(self) -> float | None:
        """Seconds until a usable route frees up, if every usable route is rate-limited."""
        now = self._clock()
        alive = self._alive()
        waits = [self._cooldown_until.get(_key(r), 0.0) - now for r in alive]
        if not alive or any(w <= 0 for w in waits):
            return None  # nothing left, or something failed for another reason
        return min(waits) + 1.0

    def _ask_chain(
        self,
        messages: list[Message],
        response_model: type[T] | None,
        temperature: float,
    ) -> LLMReply[T]:
        errors: dict[str, str] = {}
        for route in self._alive():
            key = _key(route)
            if key in self._dead or route.name in self._disabled:
                continue  # ruled out earlier in this same request
            wait = self._cooldown_until.get(key, 0.0) - self._clock()
            if wait > 0:
                errors[key] = f"rate-limited, cooling down for {wait:.0f}s"
                continue
            try:
                return self._ask_provider(route, messages, response_model, temperature)
            except BudgetExceeded:
                raise
            except (_Retryable, _Fatal, _InvalidReply) as exc:
                if isinstance(exc, _Retryable) and exc.quota:
                    # Limits are per model: this model rests, the next one is tried.
                    cooldown = exc.retry_after or DEFAULT_COOLDOWN_S
                    self._cooldown_until[key] = self._clock() + cooldown
                elif isinstance(exc, _Fatal) and exc.status in (401, 403):
                    self._disabled.add(route.name)  # bad key: no model of it will work
                elif isinstance(exc, _Fatal) and exc.status == 404:
                    self._dead.add(key)  # model retired or unknown: try the next one
                errors[key] = str(exc)
                self.usage.fallbacks += 1
                self.usage.problems[f"{key}: {str(exc)[:70]}"] += 1
        raise LLMUnavailable(errors)

    # --- internals ---

    def _ask_provider(
        self,
        provider: Provider,
        messages: list[Message],
        response_model: type[T] | None,
        temperature: float,
    ) -> LLMReply[T]:
        convo = list(messages)
        last_error = ""
        for _attempt in range(MAX_REPAIRS + 1):
            text = self._complete_with_retries(provider, convo, response_model, temperature)
            if response_model is None:
                self._cache_put(provider, messages, temperature, text)
                return LLMReply(text, provider.name, provider.model, False)
            try:
                parsed = _validate(text, response_model)
            except (ValueError, ValidationError) as exc:
                last_error = _short_error(exc)
                self.usage.repairs += 1
                self.usage.problems[f"{provider.name} invalid reply: {last_error[:70]}"] += 1
                convo += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": f"That reply was not valid: {last_error}\n"
                        "Reply again with ONLY a JSON object that matches the schema.",
                    },
                ]
                continue
            self._cache_put(provider, messages, temperature, text)
            return LLMReply(text, provider.name, provider.model, False, parsed)
        raise _InvalidReply(f"invalid JSON after {MAX_REPAIRS} repairs ({last_error})")

    def _complete_with_retries(
        self,
        provider: Provider,
        messages: list[Message],
        response_model: type[BaseModel] | None,
        temperature: float,
    ) -> str:
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self._complete(provider, messages, response_model, temperature)
            except _Retryable as exc:
                if attempt == MAX_RETRIES or exc.quota:
                    raise
                self.usage.retries += 1
                self.usage.problems[f"{provider.name}: {str(exc)[:70]}"] += 1
                delay = exc.retry_after or BACKOFF_BASE_S * (2**attempt)
                self._sleep(min(delay, MAX_BACKOFF_S))
        raise AssertionError("unreachable")

    def _complete(
        self,
        provider: Provider,
        messages: list[Message],
        response_model: type[BaseModel] | None,
        temperature: float,
    ) -> str:
        if self.max_calls is not None and self.usage.answered >= self.max_calls:
            raise BudgetExceeded(f"LLM call budget of {self.max_calls} used up")
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_model is not None and provider.json_mode:
            payload["response_format"] = {"type": "json_object"}

        self.usage.calls += 1
        self.usage.by_provider[provider.name] = self.usage.by_provider.get(provider.name, 0) + 1
        try:
            resp = self._client.post(
                f"{provider.base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {provider.api_key}",
                    "User-Agent": USER_AGENT_SUFFIX,
                    **provider.extra_headers,
                },
            )
        except httpx.HTTPError as exc:
            raise _Retryable(f"network error: {type(exc).__name__}") from exc

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = _retry_after(resp.headers.get("retry-after")) or _retry_delay(resp.text)
            raise _Retryable(
                f"HTTP {resp.status_code}: {_error_message(resp)}",
                retry_after=retry_after,
                quota=resp.status_code == 429,
            )
        if resp.status_code >= 400:
            raise _Fatal(f"HTTP {resp.status_code}: {_error_message(resp)}", resp.status_code)

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _Retryable("malformed response from provider") from exc
        self.usage.answered += 1
        usage = data.get("usage") or {}
        self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
        if not text.strip():
            raise _Retryable("empty reply")
        return str(text)

    # --- cache ---

    def _cache_key(self, provider: Provider, messages: list[Message], temperature: float) -> str:
        blob = json.dumps(
            {"model": provider.model, "messages": messages, "temperature": temperature},
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def _cache_get(
        self, provider: Provider, messages: list[Message], temperature: float
    ) -> str | None:
        if self.store is None:
            return None
        return self.store.cache_get(self._cache_key(provider, messages, temperature))

    def _cache_put(
        self, provider: Provider, messages: list[Message], temperature: float, text: str
    ) -> None:
        if self.store is not None:
            key = self._cache_key(provider, messages, temperature)
            self.store.cache_put(key, provider.name, provider.model, text)


class _InvalidReply(Exception):
    pass


def _key(route: Provider) -> str:
    return f"{route.name}/{route.model}"


# --- JSON helpers ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def extract_json(text: str) -> Any:
    """Parse JSON from a reply, tolerating ```json fences and text around one object."""
    text = text.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _validate(text: str, model: type[T]) -> T:
    return model.model_validate(extract_json(text))


def _parse(text: str, model: type[T] | None) -> T | None:
    if model is None:
        return None
    try:
        return _validate(text, model)
    except (ValueError, ValidationError):
        return None


def _with_json_instructions(messages: list[Message], model: type[BaseModel]) -> list[Message]:
    schema = json.dumps(model.model_json_schema(), separators=(",", ":"))
    instruction = (
        "Respond with ONLY a JSON object (no prose, no code fences) that matches this "
        f"JSON schema:\n{schema}"
    )
    if messages and messages[0]["role"] == "system":
        first = {**messages[0], "content": messages[0]["content"] + "\n\n" + instruction}
        return [first, *messages[1:]]
    return [{"role": "system", "content": instruction}, *messages]


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errs = exc.errors()[:3]
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'root'}: {e['msg']}" for e in errs)
    return str(exc)[:200]


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def _retry_delay(body: str) -> float | None:
    """Gemini puts the wait time in the error body: "retryDelay": "37s"."""
    match = _RETRY_DELAY_RE.search(body)
    return float(match.group(1)) if match else None


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(data, list) and data:  # Gemini wraps errors in a list
        data = data[0]
    err = data.get("error") if isinstance(data, dict) else None
    msg = err.get("message") if isinstance(err, dict) else err
    return str(msg or resp.text)[:200]
