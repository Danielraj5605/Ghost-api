"""Call endpoints directly, outside the browser, to test hypotheses (FR-23).

Safety rules:
  - Only read-only requests are replayed: GET/HEAD, and POSTs whose URL says they are
    searches or queries (e.g. Algolia, GraphQL queries). Never PUT, PATCH or DELETE.
  - Every request goes through the shared rate limiter (NFR-18).
  - robots.txt is checked per host (NFR-17).
  - Original headers (including cookies) are reused from memory and never stored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from ghost_api.capture import filters, personal
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.net import RateLimiter, RobotsPolicy, fetch_robots
from ghost_api.netguard import (
    NetworkPolicy,
    PolicyDenied,
    ResponseTooLarge,
    guarded_client,
    read_limited,
)

# Whole words in the URL (path segments split on punctuation and camelCase). A POST is
# read-only only if a READ word is present and no WRITE word is. Substring matching was a
# bug: "/api/lists/7/delete" and "/api/wishlist/add" passed because they contain "list".
READ_WORDS = {
    "search", "query", "queries", "graphql", "filter", "filters", "find", "lookup",
    "autocomplete", "suggest", "suggestions", "typeahead", "list", "browse", "count", "facets",
}  # fmt: skip
WRITE_WORDS = {
    "delete", "remove", "destroy", "erase", "add", "create", "insert", "update", "edit",
    "modify", "save", "submit", "send", "cancel", "buy", "purchase", "pay", "checkout", "cart",
    "order", "subscribe", "unsubscribe", "follow", "unfollow", "like", "unlike", "vote",
    "upvote", "downvote", "logout", "signout", "login", "signin", "register", "signup",
    "reset", "upload", "import", "publish", "archive", "restore", "move", "rename", "share",
    "invite", "approve", "reject", "transfer", "withdraw", "deposit", "block", "report",
    "flag", "hide", "mark", "set", "toggle", "enable", "disable", "apply", "claim", "redeem",
    "book", "reserve", "rate", "review", "comment", "reply", "post",
}  # fmt: skip
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# GraphQL mutations change data even though they go to the same URL as queries.
_MUTATION_RE = re.compile(r"^\s*mutation\b", re.I | re.M)
DROP_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
TIMEOUT_S = 20.0


class ReplayRefused(Exception):
    """The request is not safe or not allowed to replay."""


@dataclass
class ReplayResult:
    status: int
    body: Any | None  # parsed JSON, or None
    error: str | None = None
    captured: CapturedRequest | None = None  # redacted record of this call
    hidden: list[dict[str, Any]] = field(default_factory=list)  # personal data hidden

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300 and self.body is not None


def url_words(url: str) -> set[str]:
    """Lower-case words in a URL's path and query values: '/api/wishList/add?x=a' ->
    {'api', 'wish', 'list', 'add', 'a'}."""
    parts = urlsplit(url)
    text = parts.path + " " + " ".join(v for _, v in parse_qsl(parts.query))
    text = _CAMEL_RE.sub(" ", text)
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", text) if w and not w.isdigit()}


def is_replayable(raw: RawRequest) -> bool:
    """Only requests that read data. Anything with a write word in its URL is refused,
    even a GET, because some sites change data with GET (`/logout`, `/item/7/delete`)."""
    method = raw.method.upper()
    words = url_words(raw.url)
    if words & WRITE_WORDS:
        return False
    if method in ("GET", "HEAD"):
        return True
    if method == "POST" and words & READ_WORDS:
        return not (raw.body and _MUTATION_RE.search(_graphql_query(raw.body)))
    return False


def _graphql_query(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body
    return str(data.get("query", "")) if isinstance(data, dict) else ""


def with_overrides(
    raw: RawRequest,
    query: dict[str, str | None] | None = None,
    body: dict[str, Any] | None = None,
    drop_headers: set[str] | None = None,
) -> RawRequest:
    """A copy of `raw` with query params / top-level JSON body keys changed, and headers
    removed. A value of None removes a query parameter."""
    url = raw.url
    if query:
        parts = urlsplit(url)
        pairs = dict(parse_qsl(parts.query, keep_blank_values=True))
        for k, v in query.items():
            if v is None:
                pairs.pop(k, None)
            else:
                pairs[k] = v
        url = urlunsplit(parts._replace(query=urlencode(pairs)))
    new_body = raw.body
    if body and raw.body:
        try:
            data = json.loads(raw.body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data.update(body)
            new_body = json.dumps(data)
    drop = {h.lower() for h in (drop_headers or set())}
    headers = {k: v for k, v in raw.headers.items() if k.lower() not in drop}
    return RawRequest(raw.method, url, headers, new_body)


def with_path_params(raw: RawRequest, template: str, values: dict[str, str]) -> RawRequest:
    """Fill path parameters: template /indexes/{index}/query + {"index": "Comments"}."""
    parts = urlsplit(raw.url)
    segments = parts.path.split("/")
    names = template.split("/")
    if len(segments) != len(names):
        return raw
    for i, name in enumerate(names):
        if name.startswith("{") and name.endswith("}") and name[1:-1] in values:
            segments[i] = quote(values[name[1:-1]], safe="")
    url = urlunsplit(parts._replace(path="/".join(segments)))
    return RawRequest(raw.method, url, raw.headers, raw.body)


def body_keys(raw: RawRequest) -> set[str]:
    try:
        data = json.loads(raw.body or "")
    except ValueError:
        return set()
    return set(data) if isinstance(data, dict) else set()


def typed_value(value: str) -> object:
    """'2' -> 2 and 'true' -> True for JSON bodies; anything else stays a string."""
    try:
        return json.loads(value)
    except ValueError:
        return value


def apply_params(
    sample: RawRequest, path_template: str, path_names: set[str], params: dict[str, str]
) -> RawRequest:
    """Put each parameter where the endpoint expects it: the path, the JSON body or the
    query string. Used by the agent's call_endpoint and the dashboard's "Try it"."""
    in_body = body_keys(sample) - path_names
    path = {k: v for k, v in params.items() if k in path_names}
    body = {k: typed_value(v) for k, v in params.items() if k in in_body}
    query: dict[str, str | None] = {
        k: v for k, v in params.items() if k not in path_names and k not in in_body
    }
    request = with_path_params(sample, path_template, path)
    return with_overrides(request, query=query or None, body=body or None)


class Replayer:
    def __init__(
        self,
        target_host: str,
        limiter: RateLimiter,
        robots: RobotsPolicy | None = None,
        client: httpx.Client | None = None,
        robots_fetcher: Callable[[str], RobotsPolicy] = fetch_robots,
        max_requests: int = 80,
        policy: NetworkPolicy | None = None,
    ) -> None:
        """`policy`: where requests may go, checked on every redirect hop (default: decided
        from the target, so a public site can't send replays to a private address)."""
        self.target_host = target_host
        self.limiter = limiter
        self._robots: dict[str, RobotsPolicy] = {}
        if robots is not None:
            self._robots[robots.host] = robots
        self._fetch_robots = robots_fetcher
        self.policy = policy or NetworkPolicy.for_target(f"https://{target_host}/")
        self._client = client or guarded_client(
            self.policy, timeout=TIMEOUT_S, follow_redirects=False
        )
        self.max_requests = max_requests
        self.requests_made = 0

    def close(self) -> None:
        self._client.close()

    def robots_allows(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if host not in self._robots:
            self._robots[host] = self._fetch_robots(url)
        return self._robots[host].allows(url)

    def send(self, raw: RawRequest, trusted_read: bool = False) -> ReplayResult:
        """Send a read-only request. `trusted_read`: the user checked this endpoint only
        reads (e.g. a POST search, see `generate --allow`); otherwise the URL must say so."""
        if not trusted_read and not is_replayable(raw):
            raise ReplayRefused(f"{raw.method} requests that may change data are never replayed")
        if self.requests_made >= self.max_requests:
            raise ReplayRefused(f"probe budget of {self.max_requests} requests used up")
        if not self.robots_allows(raw.url):
            raise ReplayRefused("disallowed by robots.txt")

        self.limiter.wait()
        self.requests_made += 1
        # Browsers report HTTP/2 pseudo-headers (":authority", ":path"...); they can't be
        # sent as normal headers, and httpx derives them from the URL anyway.
        headers = {
            k: v
            for k, v in raw.headers.items()
            if k.lower() not in DROP_HEADERS and not k.startswith(":")
        }
        try:  # the default client checks the destination of every hop (netguard.py)
            with self._client.stream(
                raw.method,
                raw.url,
                headers=headers,
                content=raw.body.encode() if raw.body else None,
            ) as resp:
                text = read_limited(resp).text
        except PolicyDenied as exc:
            raise ReplayRefused(str(exc)) from exc
        except ResponseTooLarge as exc:
            return ReplayResult(resp.status_code, None, str(exc))
        except httpx.HTTPError as exc:
            return ReplayResult(0, None, f"network error: {type(exc).__name__}")

        try:
            body = filters.parse_response_json(text)
        except ValueError:
            body = None
        ctype = resp.headers.get("content-type")
        stored_body, personal_data = (
            personal.hide_body(filters.redact_body(filters.strip_xssi(text), ctype))
            if body is not None
            else (None, [])
        )
        captured = CapturedRequest(
            method=raw.method,
            url=filters.redact_url(raw.url),
            status=resp.status_code,
            resource_type="probe",
            req_headers=filters.redact_headers(headers),
            req_body=filters.redact_body(raw.body, headers.get("content-type")),
            req_content_type=headers.get("content-type"),
            resp_content_type=ctype,
            resp_body=stored_body,
            personal=personal_data,
            third_party=filters.is_third_party(raw.url, self.target_host),
        )
        error = None if body is not None else "response is not JSON"
        return ReplayResult(resp.status_code, body, error, captured)
