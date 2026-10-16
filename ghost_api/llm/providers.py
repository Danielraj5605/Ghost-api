"""LLM providers. All use the OpenAI-compatible chat-completions API.

Models can be overridden in .env, e.g. GHOST_API_GEMINI_MODEL=gemini-2.5-flash-lite.
The fallback order can be changed with GHOST_API_PROVIDERS=groq,gemini,openrouter.

Local models (Ollama, LM Studio, llama.cpp, vLLM: anything OpenAI-compatible) keep every
prompt on your machine: set GHOST_API_LOCAL_URL (e.g. http://localhost:11434/v1) and
GHOST_API_LOCAL_MODEL. Then only the local model is used, unless GHOST_API_PROVIDERS lists
others too.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)  # never printed
    json_mode: bool = True  # supports response_format={"type": "json_object"}
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Tried in order when a model is retired (404) or rate-limited (429). Free-tier
    # model names change often, so one fixed name goes stale.
    fallback_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Spec:
    base_url: str
    key_env: str
    default_model: str
    json_mode: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)
    fallback_models: tuple[str, ...] = ()


SPECS: dict[str, _Spec] = {
    "gemini": _Spec(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        # The "-latest" aliases point at the newest, busiest model and often return 503.
        default_model="gemini-2.5-flash",
        fallback_models=("gemini-3.5-flash", "gemini-flash-latest"),
    ),
    # Google's free quotas are per model, so the lite model is a separate allowance
    # on the same key.
    "gemini-lite": _Spec(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        default_model="gemini-3.5-flash-lite",
        fallback_models=("gemini-3.1-flash-lite", "gemini-flash-lite-latest"),
    ),
    "groq": _Spec(
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        default_model="openai/gpt-oss-120b",
        # Groq's per-minute token limits are per model.
        fallback_models=("qwen/qwen3.8-27b", "openai/gpt-oss-20b"),
    ),
    "openrouter": _Spec(
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        default_model="nvidia/nemotron-3-super-120b-a12b:free",
        fallback_models=("google/gemma-4-31b-it:free", "google/gemma-4-26b-a4b-it:free"),
        # Free models vary in JSON-mode support; rely on instructions + validation.
        json_mode=False,
        extra_headers={"X-Title": "Ghost API"},
    ),
}

DEFAULT_ORDER = ("gemini", "groq", "gemini-lite", "openrouter")
LOCAL = "local"


def local_provider() -> Provider | None:
    """The local OpenAI-compatible model from GHOST_API_LOCAL_URL, if one is set."""
    url = os.getenv("GHOST_API_LOCAL_URL", "").strip().rstrip("/")
    if not url:
        return None
    return Provider(
        name=LOCAL,
        base_url=url,
        model=os.getenv("GHOST_API_LOCAL_MODEL") or "llama3.1",
        # Local servers usually need no key; the gateway only needs a value to send one.
        api_key=os.getenv("GHOST_API_LOCAL_KEY") or "local",
        json_mode=os.getenv("GHOST_API_LOCAL_JSON_MODE", "") == "1",
    )


def default_providers() -> list[Provider]:
    """Providers in fallback order. Ones without an API key are skipped by the gateway."""
    order = os.getenv("GHOST_API_PROVIDERS")
    local = local_provider()
    if not order and local is not None:
        return [local]  # private by default: nothing goes to a cloud provider
    names = [n.strip() for n in order.split(",") if n.strip()] if order else list(DEFAULT_ORDER)
    providers: list[Provider] = []
    for name in names:
        if name == LOCAL:
            if local is None:
                raise ValueError("GHOST_API_PROVIDERS lists 'local' but GHOST_API_LOCAL_URL "
                                 "is not set")  # fmt: skip
            providers.append(local)
            continue
        spec = SPECS.get(name)
        if spec is None:
            raise ValueError(f"Unknown LLM provider '{name}'. Known: {', '.join(SPECS)}")
        providers.append(
            Provider(
                name=name,
                base_url=spec.base_url,
                model=os.getenv(f"GHOST_API_{name.upper().replace('-', '_')}_MODEL")
                or spec.default_model,
                api_key=os.getenv(spec.key_env) or None,
                json_mode=spec.json_mode,
                extra_headers=dict(spec.extra_headers),
                fallback_models=spec.fallback_models,
            )
        )
    return providers
