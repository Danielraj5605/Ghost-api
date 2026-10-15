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

from ghost_api.capture import filters, personal
from ghost_api.capture.models import CapturedRequest, RawRequest

MAX_BODY_CHARS = 500_000
MAX_CAPTURE_BYTES = 5_000_000  # one reply bigger than this is not read (response-too-large)
RUN_CAPTURE_BUDGET = 200_000_000  # bodies read in one run; beyond it nothing more is read


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
        self.bytes_read = 0

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
            declared = resp_headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_CAPTURE_BYTES:
                self.dropped["response-too-large"] += 1  # never read into memory
                return None
            if self.bytes_read > RUN_CAPTURE_BUDGET:
                self.dropped["capture-budget-used"] += 1
                return None
            body_bytes = response.body()
            post_bytes = request.post_data_buffer
        except PlaywrightError:
            # Body is gone (page navigated away, request aborted, ...).
            self.dropped["no-body"] += 1
            return None

        self.bytes_read += len(body_bytes)
        built = build_capture(
            request.method, request.url, request.resource_type, response.status,
            req_headers, post_bytes, resp_headers, body_bytes, self.target_host,
        )  # fmt: skip
        if isinstance(built, str):
            self.dropped[built] += 1
            return None
        return built


def build_capture(
    method: str,
    url: str,
    resource_type: str,
    status: int,
    req_headers: dict[str, str],
    post_bytes: bytes | None,
    resp_headers: dict[str, str],
    body_bytes: bytes,
    target_host: str,
) -> tuple[CapturedRequest, RawRequest] | str:
    """One request and reply, filtered and made safe to store: secrets redacted, personal
    data hidden. Returns why it was dropped (a short reason) when it is not a JSON API call.
    Used for the browser's traffic and for imported HAR files alike."""
    req_headers = {k.lower(): v for k, v in req_headers.items()}
    resp_headers = {k.lower(): v for k, v in resp_headers.items()}
    if len(body_bytes) > MAX_CAPTURE_BYTES:
        # The body is not decoded, parsed or stored, so a partial reply never becomes a schema.
        return "response-too-large"
    # Binary bodies (protobuf, images sent over fetch...) are not JSON APIs.
    body = _utf8(body_bytes)
    post_data = _utf8(post_bytes) if post_bytes else None
    if body is None or (post_bytes and post_data is None):
        return "binary"
    resp_ctype = resp_headers.get("content-type")
    if not filters.is_json_content_type(resp_ctype) and not _looks_like_json(body):
        return "non-json"
    body = filters.strip_xssi(body)
    if body.strip() and not _looks_like_json(body):
        # Says JSON but isn't (e.g. Google's length-prefixed batchexecute envelope).
        return "invalid-json"

    # Redact the full body first: truncated JSON can't be parsed for key-based redaction.
    safe_body = filters.redact_body(body, resp_ctype) or ""
    hidden_body, personal_data = personal.hide_body(safe_body)
    safe_body = hidden_body or ""
    truncated = len(safe_body) > MAX_BODY_CHARS
    if truncated:
        safe_body = safe_body[:MAX_BODY_CHARS]

    req_ctype = req_headers.get("content-type")
    raw = RawRequest(method, url, dict(req_headers), post_data)
    captured = CapturedRequest(
        method=method,
        url=filters.redact_url(url),
        status=status,
        resource_type=resource_type,
        req_headers=filters.redact_headers(req_headers),
        req_body=filters.redact_body(post_data, req_ctype),
        req_content_type=req_ctype,
        resp_content_type=resp_ctype,
        resp_body=safe_body,
        truncated=truncated,
        third_party=filters.is_third_party(url, target_host),
        personal=personal_data,
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
