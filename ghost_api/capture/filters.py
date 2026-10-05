"""Decide which captured requests are API traffic, and strip secrets out of them.

Everything here is plain, deterministic code. Redaction runs before anything is stored,
logged or sent to an LLM (FR-16, NFR-13).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

# Resource types Playwright reports for script-initiated requests.
API_RESOURCE_TYPES = {"xhr", "fetch"}

# Third-party analytics / tracking / monitoring hosts. Matched as a domain suffix.
NOISE_DOMAINS = (
    "google-analytics.com",
    "analytics.google.com",
    "googletagmanager.com",
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "facebook.net",
    "facebook.com",
    "connect.facebook.net",
    "segment.io",
    "segment.com",
    "mixpanel.com",
    "amplitude.com",
    "hotjar.com",
    "hotjar.io",
    "clarity.ms",
    "fullstory.com",
    "sentry.io",
    "nr-data.net",
    "newrelic.com",
    "datadoghq.com",
    "browser-intake-datadoghq.com",
    "intercom.io",
    "launchdarkly.com",
    "optimizely.com",
    "bing.com",
    "linkedin.com",
    "tiktok.com",
    "twitter.com",
    "ads-twitter.com",
)

# Subdomains that carry tracking or monitoring, even on the target's own domain.
NOISE_SUBDOMAINS = {
    "telemetry", "analytics", "metrics", "stats", "tracking", "track", "log", "logs", "rum",
    "beacon", "pixel", "collector", "sentry",
}  # fmt: skip

# Second-level public suffixes, so "shop.example.co.uk" maps to "example.co.uk".
_TWO_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.in", "net.in",
    "org.in", "co.jp", "ne.jp", "com.br", "com.cn", "com.mx", "co.nz", "co.za", "com.sg",
}  # fmt: skip

_SENSITIVE_WORDS = {
    "token", "secret", "password", "passwd", "pwd", "apikey", "csrf", "xsrf", "session",
    "sessionid", "sessid", "sid", "auth", "authorization", "signature", "sig", "cookie",
    "jwt", "otp", "bearer", "credential", "credentials",
}  # fmt: skip
# Run-together names like PHPSESSID, csrftoken, authtoken.
_SENSITIVE_SUFFIXES = ("sessid", "sessionid", "token", "secret", "password", "apikey")
# "key" alone is often a legitimate ID field, so only these combinations count.
_SENSITIVE_KEY_PREFIXES = {"api", "access", "private", "secret", "client", "auth", "signing"}

_IP_RE = re.compile(r"\d{1,3}(\.\d{1,3}){3}")

# Beacon-style endpoints that sites host on their own domain.
_FIRST_PARTY_NOISE_RE = re.compile(r"/(collect|beacon|telemetry|pixel|__track|rum)(/|$)", re.I)

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def site_of(host: str) -> str:
    """Approximate the registrable domain: 'api.shop.example.com' -> 'example.com'."""
    host = host.lower().rstrip(".").split(":")[0]
    if _IP_RE.fullmatch(host):
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last_two = ".".join(parts[-2:])
    if last_two in _TWO_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return last_two


def is_same_site(url: str, target_host: str) -> bool:
    """True if the URL is on the target's domain or one of its subdomains (FR-10)."""
    host = (urlsplit(url).hostname or "").lower()
    return bool(host) and site_of(host) == site_of(target_host)


def is_noise_domain(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host.split(".", 1)[0] in NOISE_SUBDOMAINS:
        return True
    return any(host == d or host.endswith("." + d) for d in NOISE_DOMAINS)


def is_json_content_type(content_type: str | None) -> bool:
    return bool(content_type) and "json" in content_type.lower()  # type: ignore[union-attr]


def classify(resource_type: str, url: str, target_host: str) -> str | None:
    """Return None if this request should be kept, otherwise the reason it was dropped."""
    if resource_type not in API_RESOURCE_TYPES:
        return f"resource:{resource_type}"
    if is_noise_domain(url):
        return "analytics"
    if not is_same_site(url, target_host):
        return "off-site"
    if _FIRST_PARTY_NOISE_RE.search(urlsplit(url).path):
        return "analytics"
    return None


# --- Redaction -----------------------------------------------------------------------------


def _name_tokens(name: str) -> list[str]:
    name = _CAMEL_RE.sub("_", name)
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def is_sensitive_name(name: str) -> bool:
    """True for header / parameter / field names that usually hold secrets.

    Splits on separators and camelCase so 'accessToken' and 'X-Auth-Token' match,
    but 'author' and 'key' do not.
    """
    tokens = _name_tokens(name)
    joined = "".join(tokens)
    if joined in _SENSITIVE_WORDS:
        return True
    for i, tok in enumerate(tokens):
        if tok in _SENSITIVE_WORDS or tok.endswith(_SENSITIVE_SUFFIXES):
            return True
        if tok == "key" and i > 0 and tokens[i - 1] in _SENSITIVE_KEY_PREFIXES:
            return True
    return False


def redact_text(text: str) -> str:
    """Mask secret-looking values (JWTs, bearer/basic credentials) inside free text."""
    text = _JWT_RE.sub(REDACTED, text)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: (REDACTED if is_sensitive_name(k) else redact_text(v)) for k, v in headers.items()}


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    safe = [(k, REDACTED if is_sensitive_name(k) else redact_text(v)) for k, v in pairs]
    return urlunsplit(parts._replace(query=urlencode(safe, safe="[]{}")))


def redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (
                REDACTED
                if is_sensitive_name(k) and not isinstance(v, dict | list)
                else redact_json(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_json(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_body(body: str | None, content_type: str | None) -> str | None:
    """Redact a request or response body, keeping its format."""
    if not body:
        return body
    ctype = (content_type or "").lower()
    stripped = body.lstrip()
    if "json" in ctype or stripped[:1] in ("{", "["):
        try:
            return json.dumps(redact_json(json.loads(body)), ensure_ascii=False)
        except ValueError:
            pass
    if "x-www-form-urlencoded" in ctype:
        pairs = parse_qsl(body, keep_blank_values=True)
        return urlencode([(k, REDACTED if is_sensitive_name(k) else v) for k, v in pairs])
    return redact_text(body)
