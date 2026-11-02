"""Replies that look usable but aren't: HTML error pages, and errors sent with HTTP 200."""

from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from ghost_api.capture.models import RawRequest
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.probe.replay import Replayer, error_in_body, not_json_error

HOST = "shop.test"


def send(response: httpx.Response) -> Any:
    replayer = Replayer(
        HOST,
        RateLimiter(10_000),
        RobotsPolicy.allow_all(HOST),
        client=httpx.Client(transport=httpx.MockTransport(lambda request: response)),
        robots_fetcher=lambda url: RobotsPolicy.allow_all(urlsplit(url).hostname or ""),
    )
    return replayer.send(RawRequest("GET", f"https://{HOST}/api/items", {}, None))


def test_a_gone_endpoint_says_404_not_just_not_json() -> None:
    result = send(httpx.Response(404, html="<html>Not found</html>"))
    assert not result.ok
    assert result.error == (
        "HTTP 404 Not Found (the endpoint seems to be gone): the reply is an HTML page, not JSON"
    )


def test_not_json_names_what_came_back() -> None:
    assert not_json_error(200, "text/plain; charset=utf-8") == (
        "HTTP 200 OK: the reply is plain text, not JSON"
    )
    assert "needs a login or a key" in not_json_error(403, "text/html")
    assert not_json_error(502, None).endswith("the reply is something else, not JSON")


def test_an_error_sent_with_http_200_is_not_data() -> None:
    body = {  # what api.wizsearch.in answers when the key header is missing
        "payload": {"params": {"x-api-key": "", "error": "API key must present in headers."}},
        "status": 0,
        "statusCode": 403,
        "message": "Please fix the issues to authenticate request.",
        "requestId": "ddc10ffd",
    }
    result = send(httpx.Response(200, json=body))
    assert not result.ok
    assert result.error == (
        "HTTP 200 OK, but the reply is an error: 403 Please fix the issues to authenticate request."
    )


@pytest.mark.parametrize(
    "body",
    [
        {"errors": [{"message": "Cannot query field"}], "data": None},
        {"error": "invalid token"},
        {"success": False, "message": "rate limited"},
        {"code": "429", "msg": "slow down"},
    ],
)
def test_error_envelopes_are_spotted(body: dict[str, Any]) -> None:
    assert error_in_body(body)


@pytest.mark.parametrize(
    "body",
    [
        {"items": [{"id": 1, "status": 404}]},  # a record with a status field
        {"status": "active", "name": "x"},
        {"error": None, "items": []},
        {"data": {"me": 1}, "errors": [{"message": "partial"}]},  # GraphQL partial data
        {"code": 500, "name": "Area 500"},  # a "code" that is data
        {"success": True, "message": "ok"},
        [{"statusCode": 403}],
        {},
    ],
)
def test_data_is_not_mistaken_for_an_error(body: Any) -> None:
    assert error_in_body(body) is None


def test_good_replies_still_pass() -> None:
    result = send(httpx.Response(200, json={"items": [{"id": 1}]}))
    assert result.ok and result.error is None
