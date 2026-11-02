"""Platform feeds: a Shopify store's /products.json, which its pages never call."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ghost_api.analysis.analyzer import analyze
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.probe.feeds import PAGE_SIZE, RETRY_WAIT_S, add_platform_feeds, is_shopify
from ghost_api.probe.replay import Replayer

HOST = "shop.test"


def capture(url: str, body: str | None = None) -> CapturedRequest:
    return CapturedRequest(
        method="POST" if body else "GET", url=url, status=200, resource_type="fetch",
        req_headers={}, req_body=body, req_content_type=None, resp_content_type=None,
        resp_body=None,
    )  # fmt: skip


SHOPIFY_TRAFFIC = [capture("https://otlp-http-production.shopifysvc.com/v1/logs")]


def store(products: int = 70, status: int = 200, robots: str = "") -> tuple[Replayer, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        url = urlsplit(str(request.url))
        if url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if url.path != "/products.json":
            return httpx.Response(404, html="<html></html>")
        if status != 200:
            return httpx.Response(status, json={"errors": "Too many requests"})
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        page, limit = int(q["page"]), int(q["limit"])
        ids = range((page - 1) * limit, min(page * limit, products))
        return httpx.Response(
            200, json={"products": [{"id": i, "title": f"P{i}", "variants": []} for i in ids]}
        )

    replayer = Replayer(
        HOST,
        RateLimiter(10_000),
        RobotsPolicy.from_text(HOST, robots) if robots else RobotsPolicy.allow_all(HOST),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        robots_fetcher=lambda url: RobotsPolicy.allow_all(urlsplit(url).hostname or ""),
    )
    return replayer, seen


def run(replayer: Replayer, traffic: list[CapturedRequest]) -> tuple[Any, list[CapturedRequest]]:
    added: list[CapturedRequest] = []
    feeds = add_platform_feeds(replayer, traffic, HOST, lambda cap, raw: added.append(cap))
    return feeds, added


def test_shopify_is_spotted_in_urls_and_bodies() -> None:
    assert is_shopify(SHOPIFY_TRAFFIC)
    assert is_shopify([capture("https://x.test/graphql", '{"shop": "acme.myshopify.com"}')])
    assert not is_shopify([capture("https://x.test/api/items")])


def test_the_feed_becomes_an_endpoint_with_paging() -> None:
    replayer, seen = store(products=70)
    feeds, added = run(replayer, SHOPIFY_TRAFFIC)
    assert [(f.records, f.error) for f in feeds] == [(70, None)]
    assert [urlsplit(u).query for u in seen if "products" in u] == [
        f"limit={PAGE_SIZE}&page=1",
        f"limit={PAGE_SIZE}&page=2",
    ]
    endpoints = analyze(added)
    assert [(e.method, e.host, e.path_template) for e in endpoints] == [
        ("GET", HOST, "/products.json")
    ]


def test_one_page_when_the_catalogue_is_small() -> None:
    replayer, seen = store(products=3)
    feeds, _ = run(replayer, SHOPIFY_TRAFFIC)
    assert feeds[0].records == 3
    assert sum("products" in u for u in seen) == 1


def test_nothing_is_sent_for_other_sites() -> None:
    replayer, seen = store()
    assert run(replayer, [capture("https://x.test/api/items")]) == ([], [])
    assert seen == []


def test_a_failure_is_reported_with_its_status() -> None:
    replayer, seen = store(status=429)
    waits: list[float] = []
    added: list[CapturedRequest] = []
    feeds = add_platform_feeds(
        replayer, SHOPIFY_TRAFFIC, HOST, lambda cap, raw: added.append(cap), sleep=waits.append
    )
    assert added == []
    assert feeds[0].error == "HTTP 429 Too Many Requests"
    assert waits == [RETRY_WAIT_S]  # waited once, retried once, then gave up
    assert sum("products" in u for u in seen) == 2


def test_robots_txt_is_respected() -> None:
    replayer, seen = store(robots="User-agent: *\nDisallow: /products.json\n")
    feeds, added = run(replayer, SHOPIFY_TRAFFIC)
    assert added == [] and feeds[0].error is not None
    assert not any("products.json" in u for u in seen)


def test_not_requested_again_when_already_captured() -> None:
    replayer, seen = store()
    traffic = [*SHOPIFY_TRAFFIC, capture(f"https://{HOST}/products.json?limit=250")]
    assert run(replayer, traffic) == ([], [])
    assert seen == []


def test_raw_requests_are_plain_gets() -> None:
    replayer, _ = store(products=1)
    raws: list[RawRequest] = []
    add_platform_feeds(replayer, SHOPIFY_TRAFFIC, HOST, lambda cap, raw: raws.append(raw))
    assert [r.method for r in raws] == ["GET"] and raws[0].body is None
