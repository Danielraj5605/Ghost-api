"""Work out why a generated test failed (FR-33)."""

from __future__ import annotations

import re
from typing import Literal

FailureKind = Literal[
    "auth",
    "not-found",
    "schema",
    "not-json",
    "rate-limited",
    "network",
    "missing-credential",
    "unknown",
]

_HTTP_RE = re.compile(r"HTTP (\d{3})")
# Real network failures by their exception names. A bare "Timeout" also appears in the
# client source shown in tracebacks (httpx.Timeout), so it would match every failure.
_NETWORK_RE = re.compile(
    r"network error|ConnectError|ConnectTimeout|ReadTimeout|WriteTimeout|PoolTimeout"
    r"|TimeoutException|RemoteProtocolError|timed out"
)
# pydantic's error layout: a dotted location line, then an indented explanation line.
_PYTEST_PREFIX_RE = re.compile(r"^E   ", re.M)
_LOC_RE = re.compile(r"^([A-Za-z0-9_.\-\[\]]+)\n {2}\S", re.M)


def classify(message: str) -> FailureKind:
    if "Missing credential" in message:
        return "missing-credential"
    if "validation error" in message and "ValidationError" in message:
        return "schema"
    if "response is not JSON" in message:
        return "not-json"
    status = _HTTP_RE.search(message)
    if status:
        code = int(status.group(1))
        if code in (401, 403):
            return "auth"
        if code in (404, 410):
            return "not-found"
        if code == 429:
            return "rate-limited"
        if code >= 500:
            return "network"
    if _NETWORK_RE.search(message):
        return "network"
    return "unknown"


def validation_locations(message: str) -> list[tuple[str, ...]]:
    """Field paths from a pydantic ValidationError, e.g. ('quotes', '0', 'author', 'name')."""
    if "validation error" not in message:
        return []
    message = _PYTEST_PREFIX_RE.sub("", message)  # pytest marks error lines with "E   "
    body = message.split("validation error", 1)[1]
    locs = []
    for match in _LOC_RE.finditer(body):
        loc = match.group(1)
        if loc.startswith(("For further", "Input")) or loc in ("E", "-"):
            continue
        locs.append(tuple(loc.split(".")))
    return list(dict.fromkeys(locs))


def short_reason(kind: FailureKind, message: str) -> str:
    status = _HTTP_RE.search(message)
    labels = {
        "auth": "the API refused the request (login or key needed)",
        "not-found": "the API said not found",
        "schema": "the response did not match the generated model",
        "not-json": "the response was not JSON",
        "rate-limited": "rate-limited by the API",
        "network": "network or server error",
        "missing-credential": "a credential is needed",
        "unknown": "unexpected failure",
    }
    text = labels[kind]
    if status and kind != "schema":
        text += f" (HTTP {status.group(1)})"
    return text
