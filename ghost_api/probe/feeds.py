"""Public catalogue feeds that a site's platform publishes but its pages never call.

A Shopify store serves its whole catalogue at /products.json, yet its pages load product
data some other way (rendered HTML, a search service), so watching the browser never sees
the feed. When the captured traffic shows a Shopify store, the feed is requested directly:
GET only, through the shared Replayer, so robots.txt, the rate limit and the request
budget apply as for every other direct call. A reply that is a product list is added as a
sample, and from then on the feed is an endpoint like any other (tested, typed, fetched).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.config import USER_AGENT_SUFFIX
from ghost_api.probe.replay import Replayer, ReplayRefused, status_text

# Seen in a Shopify store's traffic: its admin domain (often inside GraphQL bodies), the
# Shopify CDN and Shopify's telemetry hosts.
SHOPIFY_MARKERS = ("myshopify.com", "cdn.shopify.com", "shopifysvc.com", "shopifycloud.com")
RETRY_WAIT_S = 10.0  # after a 429, before the one retry
PAGE_SIZE = 50  # products per page in the samples; keeps each reply well under the size cap


@dataclass
class FeedResult:
    platform: str
    path: str
    records: int  # on the pages sampled
    error: str | None = None


def is_shopify(captured: Sequence[CapturedRequest]) -> bool:
    for item in captured:
        text = f"{item.url} {item.req_body or ''}".lower()
        if any(marker in text for marker in SHOPIFY_MARKERS):
            return True
    return False


def _request(host: str, page: int) -> RawRequest:
    url = f"https://{host}/products.json?limit={PAGE_SIZE}&page={page}"
    headers = {"accept": "application/json", "user-agent": f"Mozilla/5.0 {USER_AGENT_SUFFIX}"}
    return RawRequest("GET", url, headers, None)


def _products(body: Any) -> list[Any] | None:
    products = body.get("products") if isinstance(body, dict) else None
    return products if isinstance(products, list) else None


def add_platform_feeds(
    replayer: Replayer,
    captured: Sequence[CapturedRequest],
    host: str,
    add_sample: Callable[[CapturedRequest, RawRequest], Any],
    sleep: Callable[[float], None] = time.sleep,
) -> list[FeedResult]:
    """Request the feeds the captured traffic points to; add the replies that are product
    lists as samples. Two pages when the first is full, so paging can be told apart.
    Shopify rate-limits the feed on its own (429): wait once, then try again once."""
    if not host or host.endswith("myshopify.com") or not is_shopify(captured):
        return []
    already = any(item.url.split("?")[0].endswith(f"{host}/products.json") for item in captured)
    if already:
        return []
    records = 0
    for page in (1, 2):
        raw = _request(host, page)
        try:
            result = replayer.send(raw)
            if result.status == 429:
                sleep(RETRY_WAIT_S)
                result = replayer.send(raw)
        except ReplayRefused as exc:
            return [FeedResult("Shopify", "/products.json", records, str(exc))]
        products = _products(result.body) if result.ok else None
        if products is None or result.captured is None:
            if result.error:
                error = result.error
            elif not 200 <= result.status < 300:
                error = status_text(result.status)
            else:
                error = "the reply is not a product list"
            return [FeedResult("Shopify", "/products.json", records, error if page == 1 else None)]
        add_sample(result.captured, raw)
        records += len(products)
        if len(products) < PAGE_SIZE:
            break
    return [FeedResult("Shopify", "/products.json", records)]
