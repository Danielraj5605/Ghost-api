from typing import Any

import pytest

from ghost_api.browser.snapshot import MAX_ELEMENTS, block_reason, build_snapshot, kind_of
from ghost_api.net import RobotsPolicy

HOST = "shop.example.com"


def el(i: int, label: str = "", tag: str = "button", **kw: Any) -> dict[str, Any]:
    return {"id": i, "tag": tag, "role": "", "type": "", "label": label, "href": "",
            "download": False, "inViewport": True, **kw}  # fmt: skip


@pytest.mark.parametrize(
    "raw",
    [
        el(1, "Delete account"),
        el(2, "Log out"),
        el(3, "Sign Out"),
        el(4, "Buy now"),
        el(5, "Add to cart"),
        el(6, "Checkout"),
        el(7, "Unsubscribe"),
        el(8, "upvote"),
        el(9, "hide"),
        el(10, "", tag="a", href="https://shop.example.com/logout"),
        el(11, "x", tag="a", href="https://shop.example.com/cart/checkout"),
    ],
)
def test_destructive_elements_are_blocked(raw: dict[str, Any]) -> None:
    assert block_reason(raw, HOST) == "destructive"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (el(1, "Password", tag="input", type="password"), "sensitive input"),
        (el(2, "Upload", tag="input", type="file"), "sensitive input"),
        (el(3, "Twitter", tag="a", href="https://twitter.com/shop"), "off-site"),
        (el(4, "Email us", tag="a", href="mailto:hi@example.com"), "mailto link"),
        (el(5, "Report.pdf", tag="a", href="https://shop.example.com/r.pdf", download=True),
         "download"),
    ],
)  # fmt: skip
def test_unsafe_elements_are_blocked(raw: dict[str, Any], reason: str) -> None:
    assert block_reason(raw, HOST) == reason


@pytest.mark.parametrize(
    "raw",
    [
        el(1, "Next page"),
        el(2, "Search", tag="input", type="search"),
        el(3, "Albert Einstein", tag="a", href="https://shop.example.com/author/einstein"),
        el(4, "123 comments", tag="a", href="https://shop.example.com/item?id=1"),
        el(5, "API docs", tag="a", href="https://api.example.com/docs"),  # same site
        el(6, "Reports", tag="a", href="https://shop.example.com/reports"),
        el(7, "Posts"),
        el(8, "Log in", tag="a", href="https://shop.example.com/login"),
        el(9, "Expand", tag="a", href="javascript:void(0)"),
    ],
)
def test_normal_elements_are_allowed(raw: dict[str, Any]) -> None:
    assert block_reason(raw, HOST) is None


def test_robots_disallowed_links_are_blocked() -> None:
    robots = RobotsPolicy.from_text(HOST, "User-agent: *\nDisallow: /private/\n")
    raw = el(1, "Secret", tag="a", href="https://shop.example.com/private/x")
    assert block_reason(raw, HOST, robots) == "robots.txt"


def test_kind_of() -> None:
    assert kind_of(el(1, tag="a")) == "link"
    assert kind_of(el(1, tag="input")) == "input:text"
    assert kind_of(el(1, tag="input", type="search")) == "input:search"
    assert kind_of(el(1, tag="div", role="tab")) == "tab"
    assert kind_of(el(1, tag="div")) == "clickable"


def test_build_snapshot_filters_dedupes_sorts_and_caps() -> None:
    elements = [el(1, "Delete")]  # blocked
    elements += [el(2, "Read more", tag="a", href="https://shop.example.com/a")] * 3  # dupes
    elements += [el(3, "Below fold", inViewport=False)]
    elements += [el(i, f"Item {i}") for i in range(10, 10 + MAX_ELEMENTS + 5)]
    snap = build_snapshot(
        {"url": "https://shop.example.com/", "title": "Shop", "scroll": "top", "text": "hi",
         "elements": elements},
        HOST,
    )  # fmt: skip
    assert snap.blocked == 1
    assert len(snap.elements) == MAX_ELEMENTS
    assert [e.label for e in snap.elements].count("Read more") == 1
    assert all(e.in_viewport for e in snap.elements)  # off-screen element pushed past the cap
    assert snap.hidden == 7  # 1 off-screen + 6 over the cap
    assert snap.element(2) is not None and snap.element(1) is None


def test_describe() -> None:
    snap = build_snapshot(
        {"elements": [el(4, "Next", tag="a", href="https://shop.example.com/p?page=2"),
                      el(5, "", tag="input", type="search")]},
        HOST,
    )  # fmt: skip
    assert snap.elements[0].describe() == '[4] link "Next" -> /p?page=2'
    assert snap.elements[1].describe() == "[5] input:search"
