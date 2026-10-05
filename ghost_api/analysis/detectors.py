"""Educated guesses about an API, made from captured data alone (FR-20, FR-21).

These only produce hypotheses. The prober confirms or rejects them by calling the
endpoint (Stage 5), so a wrong guess costs a request, not a broken client.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from ghost_api.capture.filters import is_sensitive_name

PaginationStyle = Literal["page", "offset", "cursor"]

PAGE_PARAMS = {"page", "p", "pg", "pagenumber", "page_number", "pageno", "pageindex", "pagenum"}
OFFSET_PARAMS = {"offset", "start", "skip", "from"}
CURSOR_PARAMS = {
    "cursor", "after", "before", "next", "next_cursor", "nextcursor", "continuation",
    "pagetoken", "page_token", "starting_after", "since_id", "max_id", "next_page_token",
}  # fmt: skip
SIZE_PARAMS = {
    "limit", "per_page", "perpage", "pagesize", "page_size", "size", "count", "rows",
    "hitsperpage", "num", "page_limit", "take", "first",
}  # fmt: skip
# Response fields that say "there is another page".
NEXT_HINT_FIELDS = {
    "has_next", "hasnext", "has_more", "hasmore", "next", "next_page", "nextpage",
    "next_cursor", "nextcursor", "nbpages", "total_pages", "totalpages", "pages",
    "page_count", "pagecount", "total", "total_count", "totalcount", "nbhits",
}  # fmt: skip
CURSOR_RESPONSE_FIELDS = (
    "next_cursor", "nextCursor", "cursor", "next", "after", "endCursor", "next_page_token",
    "nextPageToken", "continuation",
)  # fmt: skip
ID_FIELDS = ("id", "_id", "uuid", "objectID", "objectId", "slug", "key", "url", "permalink")


@dataclass(frozen=True)
class PaginationGuess:
    style: PaginationStyle
    param: str  # the parameter to change
    location: Literal["query", "body"]
    size_param: str | None = None
    evidence: str = ""


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", name.lower())


def guess_pagination(
    query: dict[str, str], body: dict[str, Any] | None, response: Any
) -> PaginationGuess | None:
    """Guess how an endpoint pages, from one sample's parameters and response."""
    sources: list[tuple[Literal["query", "body"], dict[str, Any]]] = [("query", query)]
    if isinstance(body, dict):
        sources.append(("body", body))
    size = next((k for _, params in sources for k in params if _norm(k) in SIZE_PARAMS), None)
    styles: tuple[tuple[PaginationStyle, set[str]], ...] = (
        ("cursor", CURSOR_PARAMS),
        ("page", PAGE_PARAMS),
        ("offset", OFFSET_PARAMS),
    )
    for style, names in styles:
        for location, params in sources:
            for key in params:
                if _norm(key) in names:
                    return PaginationGuess(
                        style, key, location, size, f"request has '{key}' parameter"
                    )
    # No parameter yet, but the response says there are more pages: guess ?page=2.
    hints = sorted(k for k in top_level_keys(response) if _norm(k) in NEXT_HINT_FIELDS)
    if hints:
        return PaginationGuess("page", "page", "query", size, f"response has {', '.join(hints)}")
    return None


def next_cursor(response: Any) -> str | None:
    """Find the cursor for the next page in a response, if there is one."""
    if not isinstance(response, dict):
        return None
    candidates = [response]
    for key in ("meta", "pagination", "paging", "page_info", "pageInfo", "links"):
        if isinstance(response.get(key), dict):
            candidates.append(response[key])
    for obj in candidates:
        for key in CURSOR_RESPONSE_FIELDS:
            value = obj.get(key)
            if isinstance(value, str | int) and not isinstance(value, bool) and str(value):
                return str(value)
    return None


def top_level_keys(response: Any) -> set[str]:
    return set(response) if isinstance(response, dict) else set()


def find_items(response: Any, depth: int = 0) -> list[Any] | None:
    """The main list of records in a response: the response itself, or its largest list."""
    if isinstance(response, list):
        return response
    if not isinstance(response, dict) or depth > 2:
        return None
    best: list[Any] | None = None
    for value in response.values():
        found = value if isinstance(value, list) else find_items(value, depth + 1)
        if found is not None and (best is None or len(found) > len(best)):
            best = found
    return best


def item_fingerprints(items: list[Any]) -> list[str]:
    """Stable identity for each record: an ID-like field if present, else a content hash."""
    out = []
    for item in items:
        if isinstance(item, dict):
            ident = next((item[k] for k in ID_FIELDS if k in item and item[k] is not None), None)
            if ident is not None and not isinstance(ident, dict | list):
                out.append(f"id:{ident}")
                continue
        out.append(hashlib.sha1(json.dumps(item, sort_keys=True).encode()).hexdigest()[:12])
    return out


def shape(response: Any) -> str:
    """Coarse structure, to tell 'same kind of response' from an error page."""
    if isinstance(response, dict):
        return "object:" + ",".join(sorted(response))
    if isinstance(response, list):
        return "array"
    return type(response).__name__


# --- auth ------------------------------------------------------------------------------------

AuthStyle = Literal["none", "cookie", "bearer", "basic", "api-key-header", "api-key-query"]

# Headers that browsers add on their own; never auth, never needed in a client.
BROWSER_HEADERS = {
    "host", "content-length", "connection", "accept-encoding", "user-agent", "accept-language",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest", "sec-fetch-mode",
    "sec-fetch-site", "sec-fetch-user", "upgrade-insecure-requests", "priority", "dnt",
    "cache-control", "pragma", "te",
}  # fmt: skip


def candidate_headers(headers: dict[str, str]) -> list[str]:
    """Headers worth testing for necessity: everything except browser boilerplate."""
    return [h for h in headers if h.lower() not in BROWSER_HEADERS and not h.startswith(":")]


def auth_style(required_headers: list[str], required_query: list[str]) -> AuthStyle:
    """Name the auth style from the headers/params that turned out to be required."""
    lower = {h.lower() for h in required_headers}
    if "authorization" in lower:
        return "bearer"  # refined to basic by the caller if the scheme says so
    if any(is_sensitive_name(h) for h in lower if h != "cookie"):
        return "api-key-header"
    if any(is_sensitive_name(q) for q in required_query):
        return "api-key-query"
    if "cookie" in lower:
        return "cookie"
    return "none"


# --- page-level detection --------------------------------------------------------------------

_CHALLENGE_RE = re.compile(
    r"just a moment|checking your browser|attention required|verify you are (a )?human|"
    r"are you a robot|captcha|ddos protection|access denied|request unsuccessful|"
    r"pardon our interruption|px-captcha|datadome|unusual traffic",
    re.I,
)
_LOGIN_PATH_RE = re.compile(r"/(log-?in|sign-?in|auth|session|account/login|sso)(/|$|\?)", re.I)


def is_bot_challenge(title: str, text: str) -> bool:
    """True for bot-protection interstitials (Cloudflare, PerimeterX, DataDome...)."""
    head = f"{title} {text[:300]}"
    return bool(_CHALLENGE_RE.search(head)) and len(text) < 2000


def looks_like_login_wall(url: str, has_password_field: bool) -> bool:
    return has_password_field or bool(_LOGIN_PATH_RE.search(url))
