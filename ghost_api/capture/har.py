"""Read a HAR file (the browser's "Save all as HAR") into captured requests.

For sites Ghost API can't explore by itself: behind a login, a bot check you passed by
hand, or a flow too long for the agent. You browse; your browser records; Ghost API maps
the API from the recording, with no browser and no LLM of its own.

Every entry goes through the same filters as live capture (`recorder.build_capture`):
only JSON API calls are kept, cookies, tokens and keys are redacted, and personal data is
hidden before anything is stored. The unredacted originals stay in memory only, for
testing endpoints with direct calls. A HAR file itself holds your cookies and tokens in
plain text: delete it when you are done.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ghost_api.capture import filters
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.capture.recorder import build_capture

MAX_HAR_BYTES = 500_000_000


class HarError(Exception):
    """The file is not a HAR file Ghost API can read."""


@dataclass
class HarImport:
    start_url: str
    target_host: str
    captured: list[tuple[CapturedRequest, RawRequest]] = field(default_factory=list)
    dropped: Counter[str] = field(default_factory=Counter)
    entries: int = 0


def read_har(path: Path, start_url: str | None = None) -> HarImport:
    if path.stat().st_size > MAX_HAR_BYTES:
        raise HarError(f"{path.name} is larger than {MAX_HAR_BYTES // 1_000_000} MB")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        entries = data["log"]["entries"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HarError(f"{path.name} is not a HAR file ({exc})") from exc
    if not isinstance(entries, list):
        raise HarError(f"{path.name} has no entries")
    start = start_url or _start_url(entries)
    if not start:
        raise HarError(f"{path.name} has no http(s) requests")
    result = HarImport(start, (urlsplit(start).hostname or "").lower(), entries=len(entries))
    for entry in entries:
        try:
            item = _entry(entry, result.target_host)
        except (KeyError, TypeError, ValueError):
            result.dropped["unreadable"] += 1
            continue
        if isinstance(item, str):
            result.dropped[item] += 1
        else:
            result.captured.append(item)
    return result


def _start_url(entries: list[Any]) -> str:
    """The first page the browser loaded, or the origin of the first request."""
    for entry in entries:
        url = str(entry.get("request", {}).get("url", ""))
        if entry.get("_resourceType") == "document" and url.startswith("http"):
            return url
    for entry in entries:
        url = str(entry.get("request", {}).get("url", ""))
        if url.startswith("http"):
            parts = urlsplit(url)
            return f"{parts.scheme}://{parts.netloc}/"
    return ""


def _headers(items: list[dict[str, Any]]) -> dict[str, str]:
    # HTTP/2 pseudo-headers (":authority") are not real headers.
    return {str(h["name"]).lower(): str(h["value"]) for h in items if not h["name"].startswith(":")}


def _resource_type(entry: dict[str, Any], resp_headers: dict[str, str]) -> str:
    """Chrome and Edge write `_resourceType`; Firefox doesn't, so JSON replies count as fetch."""
    kind = entry.get("_resourceType")
    if isinstance(kind, str) and kind:
        return kind.lower()
    if filters.is_json_content_type(resp_headers.get("content-type")):
        return "fetch"
    return "other"


def _entry(entry: dict[str, Any], target_host: str) -> tuple[CapturedRequest, RawRequest] | str:
    request, response = entry["request"], entry["response"]
    url = str(request["url"])
    if not url.startswith(("http://", "https://")):
        return "not-http"
    req_headers = _headers(request.get("headers", []))
    resp_headers = _headers(response.get("headers", []))
    kind = _resource_type(entry, resp_headers)
    reason = filters.classify(kind, url, target_host)
    if reason:
        return reason
    status = int(response.get("status") or 0)
    if 300 <= status < 400:
        return "redirect"
    if status == 0:
        return "no-reply"  # blocked, cancelled or failed in the browser
    content = response.get("content", {})
    text = content.get("text")
    if text is None:
        return "no-body"  # saved without content ("Save all as HAR (sanitized)")
    body = _decode(str(text), content.get("encoding"))
    if body is None:
        return "binary"
    post = request.get("postData", {}).get("text")
    post_bytes = str(post).encode("utf-8") if post is not None else None
    if "content-type" not in resp_headers and content.get("mimeType"):
        resp_headers["content-type"] = str(content["mimeType"])
    return build_capture(
        str(request["method"]).upper(), url, kind, status, req_headers, post_bytes,
        resp_headers, body, target_host,
    )  # fmt: skip


def _decode(text: str, encoding: Any) -> bytes | None:
    if encoding == "base64":
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            return None
    return text.encode("utf-8")


def summary(result: HarImport) -> Iterator[str]:
    yield f"{result.entries} entries, {len(result.captured)} API calls kept"
    if result.dropped:
        top = ", ".join(f"{k}: {v}" for k, v in result.dropped.most_common(6))
        yield f"left out: {top}"
