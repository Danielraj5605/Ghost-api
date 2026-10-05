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
from collections.abc import Callable
from dataclasses import dataclass, field
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
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _Fatal(Exception):
    """Provider can't serve this request (bad key, bad model, bad request)."""


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hits: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)

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
    ) -> None:
        self.providers = [p for p in providers if p.api_key]
        self.store = store
        self.max_calls = max_calls
        self.usage = Usage()
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._sleep = sleep

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

        for provider in self.providers:
            cached = self._cache_get(provider, messages, temperature)
            if cached is not None:
                parsed = _parse(cached, response_model) if response_model else None
                if response_model is None or parsed is not None:
                    self.usage.cache_hits += 1
                    return LLMReply(cached, provider.name, provider.model, True, parsed)

        errors: dict[str, str] = {}
        for provider in self.providers:
            try:
                return self._ask_provider(provider, messages, response_model, temperature)
            except BudgetExceeded:
                raise
            except (_Retryable, _Fatal, _InvalidReply) as exc:
                errors[provider.name] = str(exc)
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
                if attempt == MAX_RETRIES:
                    raise
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
        if self.max_calls is not None and self.usage.calls >= self.max_calls:
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
            raise _Retryable(
                f"HTTP {resp.status_code}: {_error_message(resp)}",
                retry_after=_retry_after(resp.headers.get("retry-after")),
            )
        if resp.status_code >= 400:
            raise _Fatal(f"HTTP {resp.status_code}: {_error_message(resp)}")

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _Retryable("malformed response from provider") from exc
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
