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
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
