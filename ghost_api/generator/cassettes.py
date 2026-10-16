"""Recorded replies ("cassettes") so a generated client's tests run offline.

The generated tests replay these by default: deterministic, fast, no traffic to the site,
fine for CI. `GHOST_API_LIVE=1` runs the same tests against the real API (Ghost API's own
verification always does).

Replies come from what was stored while exploring, so cookies, tokens and keys are already
redacted and personal data hidden; every entry is checked again before it is written.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ghost_api.analysis.analyzer import endpoint_keys
from ghost_api.analysis.graphql import graphql_payload, operation_name
from ghost_api.capture.filters import REDACTED, redact_text
from ghost_api.capture.models import CapturedRequest
from ghost_api.generator.spec import ClientSpec

PER_ENDPOINT = 6  # the latest replies for each endpoint (pages, search terms...)
FILE = "cassettes.json"


def path_regex(path: str) -> str:
    """'/api/items/{item_id}' -> '/api/items/[^/]+' (matched against the request path)."""
    parts = re.split(r"\{[^}]*\}", path)
    return "[^/]+".join(re.escape(p) for p in parts)


def build(spec: ClientSpec, requests: list[CapturedRequest]) -> dict[str, Any]:
    by_key: dict[str, list[CapturedRequest]] = {}
    for key, req in zip(endpoint_keys(requests), requests, strict=False):
        by_key.setdefault(key, []).append(req)
    replies: list[dict[str, Any]] = []
    for method in spec.methods:
        usable = [r for r in by_key.get(method.endpoint_key, []) if _usable(r)]
        for req in usable[-PER_ENDPOINT:]:
            entry = _entry(method.http_method, method.path, req)
            if entry is not None:
                replies.append(entry)
    return {
        "about": "Replies recorded by Ghost API, replayed by the generated tests. "
        "Set GHOST_API_LIVE=1 to test against the real API instead.",
        "site": spec.domain,
        "replies": replies,
    }


def _usable(req: CapturedRequest) -> bool:
    return 200 <= req.status < 300 and bool(req.resp_body) and not req.truncated


def _entry(http_method: str, path: str, req: CapturedRequest) -> dict[str, Any] | None:
    url = urlsplit(req.url)
    query = {
        k: v for k, v in parse_qsl(url.query, keep_blank_values=True) if REDACTED not in v
    }  # a redacted key is not something a request can match on
    payload = graphql_payload(req.method, req.url, req.req_body)
    entry: dict[str, Any] = {
        "method": http_method,
        "path": url.path,
        "path_regex": path_regex(path),
        "query": query,
        "operation": operation_name(payload) if payload else None,
        "status": req.status,
        "content_type": req.resp_content_type or "application/json",
        "body": req.resp_body,
    }
    text = json.dumps(entry, ensure_ascii=False)
    if redact_text(text) != text:
        return None  # never write anything secret-looking, even into test data
    return entry


def dumps(cassettes: dict[str, Any]) -> str:
    return json.dumps(cassettes, indent=1, ensure_ascii=False) + "\n"
