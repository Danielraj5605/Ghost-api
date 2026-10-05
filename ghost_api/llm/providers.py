"""LLM providers. All use the OpenAI-compatible chat-completions API.

Models can be overridden in .env, e.g. GHOST_API_GEMINI_MODEL=gemini-2.5-flash-lite.
The fallback order can be changed with GHOST_API_PROVIDERS=groq,gemini,openrouter.
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


@dataclass(frozen=True)
class _Spec:
    base_url: str
    key_env: str
    default_model: str
    json_mode: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)


SPECS: dict[str, _Spec] = {
    "gemini": _Spec(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        # The "-latest" aliases point at the newest, busiest model and often return 503.
        default_model="gemini-2.5-flash",
    ),
    # Google's free quotas are per model, so the lite model is a separate allowance
    # on the same key.
    "gemini-lite": _Spec(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        default_model="gemini-2.5-flash-lite",
    ),
    "groq": _Spec(
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        default_model="openai/gpt-oss-120b",
    ),
    "openrouter": _Spec(
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        default_model="qwen/qwen3.8-27b:free",
        # Free models vary in JSON-mode support; rely on instructions + validation.
        json_mode=False,
        extra_headers={"X-Title": "Ghost API"},
    ),
}

DEFAULT_ORDER = ("gemini", "groq", "gemini-lite", "openrouter")


def default_providers() -> list[Provider]:
    """Providers in fallback order. Ones without an API key are skipped by the gateway."""
    order = os.getenv("GHOST_API_PROVIDERS")
    names = [n.strip() for n in order.split(",") if n.strip()] if order else list(DEFAULT_ORDER)
    providers: list[Provider] = []
    for name in names:
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
            )
        )
    return providers
