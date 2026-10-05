import json

import pytest

from ghost_api.capture import filters
from ghost_api.capture.filters import REDACTED


@pytest.mark.parametrize(
    ("host", "site"),
    [
        ("example.com", "example.com"),
        ("www.example.com", "example.com"),
        ("api.shop.example.com", "example.com"),
        ("shop.example.co.uk", "example.co.uk"),
        ("localhost", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
    ],
)
def test_site_of(host: str, site: str) -> None:
    assert filters.site_of(host) == site


def test_classify_keeps_same_site_fetch() -> None:
    assert filters.classify("fetch", "https://api.example.com/v1/books", "www.example.com") is None
    assert filters.classify("xhr", "https://example.com/api/quotes?page=2", "example.com") is None


@pytest.mark.parametrize(
    ("resource_type", "url", "reason"),
    [
        ("image", "https://example.com/logo.png", "resource:image"),
        ("stylesheet", "https://example.com/site.css", "resource:stylesheet"),
        ("font", "https://example.com/f.woff2", "resource:font"),
        ("document", "https://example.com/", "resource:document"),
        ("fetch", "https://www.google-analytics.com/g/collect?v=2", "analytics"),
        ("xhr", "https://o123.ingest.sentry.io/api/1/envelope/", "analytics"),
        ("fetch", "https://cdn.other-site.com/data.json", "off-site"),
        ("fetch", "https://example.com/collect?event=view", "analytics"),
        ("fetch", "https://telemetry.example.com/1/settings", "analytics"),
        ("xhr", "https://metrics.example.com/v1/batch", "analytics"),
    ],
)
def test_classify_drops_noise(resource_type: str, url: str, reason: str) -> None:
    assert filters.classify(resource_type, url, "example.com") == reason


@pytest.mark.parametrize(
    "name",
    [
        "Cookie", "Set-Cookie", "Authorization", "X-Auth-Token", "x-csrf-token", "accessToken",
        "refresh_token", "api_key", "apiKey", "X-API-Key", "client_secret", "password",
        "sessionId", "PHPSESSID", "sig", "csrftoken", "authtoken",
    ],
)  # fmt: skip
def test_sensitive_names(name: str) -> None:
    assert filters.is_sensitive_name(name)


@pytest.mark.parametrize(
    "name",
    ["author", "authors", "key", "keyword", "id", "title", "page", "monkey", "tokenizer_name"],
)
def test_non_sensitive_names(name: str) -> None:
    assert not filters.is_sensitive_name(name)


def test_redact_headers() -> None:
    out = filters.redact_headers(
        {"Cookie": "sid=fake-removed", "Authorization": "Bearer fake-removed", "Accept": "application/json"}
    )
    assert out == {"Cookie": REDACTED, "Authorization": REDACTED, "Accept": "application/json"}


def test_redact_url_query_params() -> None:
    out = filters.redact_url("https://example.com/api?page=2&api_key=fake-removed&q=dune")
    assert "fake-removed" not in out
    assert "page=2" in out and "q=dune" in out


def test_redact_url_without_query_is_unchanged() -> None:
    url = "https://example.com/api/books/123"
    assert filters.redact_url(url) == url


def test_redact_json_nested_keeps_normal_fields() -> None:
    body = {
        "author": "Albert Einstein",
        "key": "OL123W",
        "user": {"name": "a", "accessToken": "fake-removed", "password": "fake-removed"},
        "items": [{"session_id": "s1", "title": "x"}],
    }
    out = filters.redact_json(body)
    assert out["author"] == "Albert Einstein"
    assert out["key"] == "OL123W"
    assert out["user"] == {"name": "a", "accessToken": REDACTED, "password": REDACTED}
    assert out["items"] == [{"session_id": REDACTED, "title": "x"}]


def test_redact_text_masks_jwt_and_bearer() -> None:
    jwt = "fake-jwt-removed"
    text = f"token is {jwt} and header Bearer fake-token-removed"
    out = filters.redact_text(text)
    assert jwt not in out
    assert "abcdefghijklmnop" not in out


def test_redact_body_json_and_form() -> None:
    out = filters.redact_body('{"q": "dune", "token": "t"}', "application/json")
    assert json.loads(out or "") == {"q": "dune", "token": REDACTED}

    form = filters.redact_body("user=bob&password=fake-removed", "application/x-www-form-urlencoded")
    assert form is not None and "fake-removed" not in form and "user=bob" in form


def test_redact_body_handles_empty() -> None:
    assert filters.redact_body(None, "application/json") is None
    assert filters.redact_body("", None) == ""
