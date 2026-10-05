"""Data captured from the browser's network traffic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class CapturedRequest:
    """One API request/response pair. All fields are already redacted."""

    method: str
    url: str
    status: int
    resource_type: str
    req_headers: dict[str, str]
    req_body: str | None
    req_content_type: str | None
    resp_content_type: str | None
    resp_body: str | None
    truncated: bool = False
    third_party: bool = False  # served from a different domain than the target site
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class RawRequest:
    """The original, UNREDACTED request, kept in memory only so endpoints can be replayed.

    Never stored, logged or sent to an LLM (NFR-13). repr() hides headers and body.
    """

    method: str
    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    body: str | None = field(default=None, repr=False)
