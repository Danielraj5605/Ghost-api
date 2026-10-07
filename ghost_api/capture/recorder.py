"""Record API traffic from a Playwright page (FR-13, FR-14).

The 'response' event handler only queues responses; bodies are read later in `flush()`,
which the browser driver calls after every action. Reading bodies outside the event
handler keeps the sync Playwright API safe from re-entrancy problems.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response

from ghost_api.capture import filters
from ghost_api.capture.models import CapturedRequest, RawRequest

MAX_BODY_CHARS = 500_000


class TrafficRecorder:
    def __init__(
        self,
        target_host: str,
        on_capture: Callable[[CapturedRequest], None] | None = None,
    ) -> None:
        self.target_host = target_host
        self.on_capture = on_capture
        self.captured: list[CapturedRequest] = []
        # raw[i] is the unredacted original of captured[i] (memory only, for replaying).
        self.raw: list[RawRequest | None] = []
        self.dropped: Counter[str] = Counter()
        self._pending: list[Response] = []

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        request = response.request
        reason = filters.classify(request.resource_type, request.url, self.target_host)
        if reason:
            self.dropped[reason] += 1
            return
        if 300 <= response.status < 400:
            self.dropped["redirect"] += 1
            return
        self._pending.append(response)

    def flush(self) -> list[CapturedRequest]:
        """Read bodies of queued responses, keep JSON ones, redact, and store them."""
        new: list[CapturedRequest] = []
        pending, self._pending = self._pending, []
        for response in pending:
            try:
                built = self._build(response)
            except Exception:  # noqa: BLE001 - one odd response must not stop the run
                self.dropped["unreadable"] += 1
                continue
            if built is None:
                continue
            new.append(self.add(*built))
        return new

    def add(self, item: CapturedRequest, raw: RawRequest | None = None) -> CapturedRequest:
        """Record a request (from the browser, or a replay made by the prober)."""
        self.captured.append(item)
        self.raw.append(raw)
        if self.on_capture:
            self.on_capture(item)
        return item

    def _build(self, response: Response) -> tuple[CapturedRequest, RawRequest] | None:
        request = response.request
        try:
            resp_headers = response.all_headers()
            req_headers = request.all_headers()
            body_bytes = response.body()
            post_bytes = request.post_data_buffer
        except PlaywrightError:
            # Body is gone (page navigated away, request aborted, ...).
            self.dropped["no-body"] += 1
            return None

        # Binary bodies (protobuf, images sent over fetch...) are not JSON APIs. Decoding
        # them strictly here keeps them out; Playwright's .text() would raise mid-run.
        body = _utf8(body_bytes)
        post_data = _utf8(post_bytes) if post_bytes else None
        if body is None or (post_bytes and post_data is None):
            self.dropped["binary"] += 1
            return None

        resp_ctype = resp_headers.get("content-type")
        if not filters.is_json_content_type(resp_ctype) and not _looks_like_json(body):
            self.dropped["non-json"] += 1
            return None
        body = filters.strip_xssi(body)
        if body.strip() and not _looks_like_json(body):
            # Says JSON but isn't (e.g. Google's length-prefixed batchexecute envelope).
            self.dropped["invalid-json"] += 1
            return None

        # Redact the full body first: truncated JSON can't be parsed for key-based redaction.
        safe_body = filters.redact_body(body, resp_ctype) or ""
        truncated = len(safe_body) > MAX_BODY_CHARS
        if truncated:
            safe_body = safe_body[:MAX_BODY_CHARS]

        req_ctype = req_headers.get("content-type")
        raw = RawRequest(request.method, request.url, dict(req_headers), post_data)
        captured = CapturedRequest(
            method=request.method,
            url=filters.redact_url(request.url),
            status=response.status,
            resource_type=request.resource_type,
            req_headers=filters.redact_headers(req_headers),
            req_body=filters.redact_body(post_data, req_ctype),
            req_content_type=req_ctype,
            resp_content_type=resp_ctype,
            resp_body=safe_body,
            truncated=truncated,
            third_party=filters.is_third_party(request.url, self.target_host),
        )
        return captured, raw


def _utf8(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _looks_like_json(body: str) -> bool:
    stripped = body.lstrip()
    if stripped[:1] not in ("{", "["):
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True
