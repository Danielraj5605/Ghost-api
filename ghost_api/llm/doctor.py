"""Check that each configured LLM provider answers (one tiny call per provider)."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from ghost_api.llm.gateway import LLMError, LLMGateway
from ghost_api.llm.providers import Provider, default_providers


class _Ping(BaseModel):
    status: str
    sum: int


def check_providers(providers: list[Provider] | None = None) -> list[dict[str, Any]]:
    """One row per provider: name, model, key set?, ok?, detail, seconds."""
    rows: list[dict[str, Any]] = []
    for provider in providers if providers is not None else default_providers():
        if not provider.api_key:
            rows.append(
                {
                    "provider": provider.name,
                    "model": provider.model,
                    "key": False,
                    "ok": False,
                    "detail": "no API key in .env",
                    "seconds": 0.0,
                }
            )
            continue
        gateway = LLMGateway([provider])
        started = time.monotonic()
        model, ok, detail = provider.model, False, ""
        try:
            reply = gateway.ask(
                [{"role": "user", "content": 'What is 2 + 3? Set "status" to "ok".'}],
                response_model=_Ping,
            )
            model = reply.model
            ok = reply.parsed is not None and reply.parsed.sum == 5
            detail = "OK" if ok else f"odd answer: {reply.text[:60]}"
        except LLMError as exc:
            detail = str(exc)[:160]
        finally:
            gateway.close()
        rows.append(
            {
                "provider": provider.name,
                "model": model,
                "key": True,
                "ok": ok,
                "detail": detail,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
    return rows
