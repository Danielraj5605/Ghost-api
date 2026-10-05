"""Replayer and prober tests against a fake API (httpx MockTransport): no network."""

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ghost_api.analysis.models import Endpoint
from ghost_api.capture.models import RawRequest
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.probe.prober import Prober
from ghost_api.probe.replay import (
    Replayer,
    ReplayRefused,
    is_replayable,
    with_overrides,
    with_path_params,
)

HOST = "shop.test"
EP = Endpoint("GET", HOST, "/api/items")


def fake_api(request: httpx.Request) -> httpx.Response:
    """A small API with known behaviour, so each test knows the right answer."""
    url = urlsplit(str(request.url))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    if url.path == "/api/items":  # pages of 3 items, public
        page = int(q.get("page", "1"))
        return httpx.Response(
            200, json={"page": page, "items": [{"id": page * 10 + i} for i in range(3)]}
        )
    if url.path == "/api/stuck":  # ignores ?page, always the same records
        return httpx.Response(200, json={"items": [{"id": 1}, {"id": 2}]})
    if url.path == "/api/keyed":  # needs the x-api-key header
        if request.headers.get("x-api-key") != "fake-removed":
            return httpx.Response(401, json={"error": "no key"})
        return httpx.Response(200, json={"items": [{"id": 1}]})
    if url.path == "/api/session":  # needs the session cookie
        if "sid=fake-removed" not in request.headers.get("cookie", ""):
            return httpx.Response(403, json={"error": "login"})
        return httpx.Response(200, json={"me": "ada"})
    if url.path == "/api/qkey":  # needs ?apikey
        if q.get("apikey") != "fake-removed":
            return httpx.Response(401, json={"error": "key"})
        return httpx.Response(200, json={"items": [{"id": 1}]})
    if url.path == "/api/feed":  # cursor pagination
        cur = q.get("cursor", "start")
        ids = {"start": [1, 2], "c2": [3, 4]}.get(cur, [])
        return httpx.Response(200, json={"items": [{"id": i} for i in ids], "next_cursor": "c2"})
    if url.path == "/api/search" and request.method == "POST":  # paging in the JSON body
        page = json.loads(request.content)["page"]
        return httpx.Response(200, json={"hits": [{"objectID": f"{page}-{i}"} for i in range(2)]})
    if url.path == "/api/signed":  # one-time signed URL: replays fail
        return httpx.Response(403, json={"error": "expired"})
    return httpx.Response(404, json={"error": "not found"})


def make_replayer(**kw: Any) -> tuple[Replayer, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fake_api(request)

    replayer = Replayer(
        HOST,
        RateLimiter(10_000),
        RobotsPolicy.allow_all(HOST),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        robots_fetcher=lambda url: RobotsPolicy.allow_all(urlsplit(url).hostname or ""),
        **kw,
    )
    return replayer, seen


def raw(path: str, method: str = "GET", headers: dict[str, str] | None = None,
        body: Any = None) -> RawRequest:  # fmt: skip
    base = {"accept": "application/json", "user-agent": "Chrome GhostAPI/0.1"}
    return RawRequest(method, f"https://{HOST}{path}", {**base, **(headers or {})},
                      json.dumps(body) if body is not None else None)  # fmt: skip


# --- replay safety ---


@pytest.mark.parametrize(
    ("request_", "ok"),
    [
        (raw("/api/items"), True),
        (raw("/api/search", "POST", body={"q": "x"}), True),
        (raw("/graphql", "POST", body={"query": "query { me { id } }"}), True),
        (raw("/graphql", "POST", body={"query": "mutation { deleteMe }"}), False),
        (raw("/api/cart", "POST", body={"id": 1}), False),
        (raw("/api/items/1", "DELETE"), False),
        (raw("/api/items/1", "PUT"), False),
    ],
)
def test_only_read_only_requests_are_replayable(request_: RawRequest, ok: bool) -> None:
    assert is_replayable(request_) is ok


def test_unsafe_requests_are_refused_before_sending() -> None:
    replayer, seen = make_replayer()
    with pytest.raises(ReplayRefused):
        replayer.send(raw("/api/items/1", "DELETE"))
    assert seen == []


def test_robots_and_budget_are_enforced() -> None:
    replayer, seen = make_replayer(max_requests=1)
    replayer._robots[HOST] = RobotsPolicy.from_text(HOST, "User-agent: *\nDisallow: /api/\n")
    with pytest.raises(ReplayRefused, match="robots"):
        replayer.send(raw("/api/items"))
    replayer._robots[HOST] = RobotsPolicy.allow_all(HOST)
    replayer.send(raw("/api/items"))
    with pytest.raises(ReplayRefused, match="budget"):
        replayer.send(raw("/api/items"))
    assert len(seen) == 1


def test_replay_record_is_redacted() -> None:
    replayer, _ = make_replayer()
    result = replayer.send(raw("/api/keyed", headers={"x-api-key": "fake-removed"}))
    assert result.ok and result.captured is not None
    assert "fake-removed" not in json.dumps(result.captured.req_headers)
    assert result.captured.resource_type == "probe"


def test_with_overrides() -> None:
    r = raw("/api/items?page=1&q=a", "POST", headers={"cookie": "x"}, body={"page": 1, "q": "a"})
    out = with_overrides(r, query={"page": "2", "q": None}, body={"page": 2},
                         drop_headers={"Cookie"})  # fmt: skip
    assert out.url == f"https://{HOST}/api/items?page=2"
    assert json.loads(out.body or "") == {"page": 2, "q": "a"}
    assert "cookie" not in out.headers
    assert r.url.endswith("page=1&q=a")  # original untouched


# --- prober ---


def probe(sample: RawRequest, endpoint: Endpoint = EP) -> tuple[Any, list[httpx.Request]]:
    replayer, seen = make_replayer()
    added: list[Any] = []
    report = Prober(replayer, on_sample=lambda c, r: added.append(c)).probe(endpoint, [sample])
    report.added = added  # type: ignore[attr-defined]
    return report, seen


def test_page_pagination_is_confirmed_and_public_endpoint_needs_nothing() -> None:
    report, seen = probe(raw("/api/items?page=1", headers={"cookie": "sid=fake-removed"}))
    assert report.replayable
    assert report.pagination == {"style": "page", "param": "page", "location": "query",
                                 "size_param": None, "confirmed": True}  # fmt: skip
    assert report.auth_type == "none" and report.required_headers == []
    results = {h.text: h.result for h in report.hypotheses}
    assert results["needs none of its custom headers or cookies"] == "confirmed"
    assert any("page=2" in str(r.url) for r in seen)
    assert len(report.added) == 1  # page 2 became a new sample
    assert len(seen) == 3  # baseline + next page + one "drop all headers" call


def test_pagination_that_does_nothing_is_rejected() -> None:
    report, _ = probe(raw("/api/stuck?page=1"), Endpoint("GET", HOST, "/api/stuck"))
    assert report.pagination is None
    page_h = next(h for h in report.hypotheses if "paginates" in h.text)
    assert page_h.result == "rejected" and "same records" in page_h.evidence


def test_cursor_pagination() -> None:
    report, seen = probe(raw("/api/feed?cursor=start"), Endpoint("GET", HOST, "/api/feed"))
    assert report.pagination is not None and report.pagination["style"] == "cursor"
    assert report.pagination["confirmed"]
    assert any("cursor=c2" in str(r.url) for r in seen)


def test_pagination_in_json_body() -> None:
    sample = raw("/api/search", "POST", body={"query": "x", "page": 0})
    report, seen = probe(sample, Endpoint("POST", HOST, "/api/search"))
    assert report.pagination is not None
    assert (report.pagination["location"], report.pagination["confirmed"]) == ("body", True)
    assert json.loads(seen[1].content)["page"] == 1  # stays an integer


def test_required_api_key_header_is_found() -> None:
    sample = raw("/api/keyed", headers={"x-api-key": "fake-removed", "referer": "https://shop.test/"})
    report, _ = probe(sample, Endpoint("GET", HOST, "/api/keyed"))
    assert report.required_headers == ["x-api-key"]
    assert report.auth_type == "api-key-header"
    assert "fake-removed" not in json.dumps([h.__dict__ for h in report.hypotheses])


def test_required_cookie_is_found() -> None:
    sample = raw("/api/session", headers={"cookie": "sid=fake-removed; theme=dark", "x-requested-with": "x"})
    report, _ = probe(sample, Endpoint("GET", HOST, "/api/session"))
    assert report.required_headers == ["cookie"]
    assert report.auth_type == "cookie"


def test_required_secret_query_param() -> None:
    report, _ = probe(raw("/api/qkey?apikey=fake-removed&q=x"), Endpoint("GET", HOST, "/api/qkey"))
    assert report.required_query == ["apikey"]
    assert report.auth_type == "api-key-query"


def test_endpoint_that_cannot_be_replayed() -> None:
    report, seen = probe(raw("/api/signed?sig=x"), Endpoint("GET", HOST, "/api/signed"))
    assert report.replayable is False
    assert report.hypotheses[0].result == "rejected" and "403" in report.hypotheses[0].evidence
    assert len(seen) == 1  # no further tests on a dead endpoint


def test_unsafe_samples_are_never_probed() -> None:
    report, seen = probe(
        raw("/api/cart", "POST", body={"id": 1}), Endpoint("POST", HOST, "/api/cart")
    )
    assert seen == [] and not report.replayable
    assert "read-only" in report.note


def test_http2_pseudo_headers_are_not_sent() -> None:
    replayer, seen = make_replayer()
    pseudo = {":authority": HOST, ":method": "GET", ":path": "/api/items", ":scheme": "https"}
    assert replayer.send(raw("/api/items", headers=pseudo)).ok
    assert not any(k.startswith(":") for k in seen[0].headers)


def test_with_path_params() -> None:
    r = raw("/1/indexes/Item_dev/query?x=1")
    out = with_path_params(r, "/1/indexes/{index}/query", {"index": "Comment dev"})
    assert out.url == f"https://{HOST}/1/indexes/Comment%20dev/query?x=1"
    assert with_path_params(r, "/other/{id}", {"id": "1"}).url == r.url  # shape mismatch
