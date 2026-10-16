import pytest

from ghost_api.analysis import detectors as d
from tests.fake_secrets import SESSION_ID


@pytest.mark.parametrize(
    ("query", "body", "response", "expected"),
    [
        ({"page": "1"}, None, {}, ("page", "page", "query", None)),
        (
            {"q": "x", "per_page": "20", "page": "3"},
            None,
            {},
            ("page", "page", "query", "per_page"),
        ),
        ({"offset": "0", "limit": "25"}, None, {}, ("offset", "offset", "query", "limit")),
        ({"cursor": "abc"}, None, {}, ("cursor", "cursor", "query", None)),
        (
            {},
            {"query": "x", "page": 0, "hitsPerPage": 20},
            {},
            ("page", "page", "body", "hitsPerPage"),
        ),
        ({}, None, {"results": [], "has_next": True}, ("page", "page", "query", None)),
    ],
)
def test_guess_pagination(
    query: dict[str, str],
    body: dict[str, object] | None,
    response: object,
    expected: tuple[str, ...],
) -> None:
    guess = d.guess_pagination(query, body, response)
    assert guess is not None
    assert (guess.style, guess.param, guess.location, guess.size_param) == expected


def test_no_pagination() -> None:
    assert d.guess_pagination({"q": "dune"}, None, {"title": "Dune"}) is None


@pytest.mark.parametrize(
    ("response", "cursor"),
    [
        ({"items": [], "next_cursor": "c2"}, "c2"),
        ({"data": [], "meta": {"nextCursor": "xyz"}}, "xyz"),
        ({"pageInfo": {"endCursor": "e1"}}, "e1"),
        ({"items": [], "next": None}, None),
        ([1, 2], None),
    ],
)
def test_next_cursor(response: object, cursor: str | None) -> None:
    assert d.next_cursor(response) == cursor


def test_find_items_picks_the_main_list() -> None:
    assert d.find_items([1, 2]) == [1, 2]
    body = {"tags": ["a"], "data": {"quotes": [{"id": 1}, {"id": 2}, {"id": 3}]}}
    assert d.find_items(body) == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert d.find_items({"title": "x"}) is None


def test_item_fingerprints_prefer_ids() -> None:
    fps = d.item_fingerprints([{"id": 7, "t": "a"}, {"objectID": "x"}, {"t": "no id"}])
    assert fps[:2] == ["id:7", "id:x"]
    assert len(fps[2]) == 12
    assert d.item_fingerprints([{"t": 1}]) == d.item_fingerprints([{"t": 1}])


def test_shape() -> None:
    assert d.shape({"b": 1, "a": 2}) == "object:a,b"
    assert d.shape([]) == "array"
    assert d.shape(None) == "NoneType"


def test_candidate_headers_skip_browser_boilerplate() -> None:
    headers = {
        "user-agent": "x",
        "accept-language": "en",
        "sec-fetch-mode": "cors",
        "cookie": f"s={SESSION_ID}",
        "x-api-key": "k",
        "referer": "https://e.com/",
        "accept": "*/*",
    }
    assert d.candidate_headers(headers) == ["cookie", "x-api-key", "referer", "accept"]


@pytest.mark.parametrize(
    ("headers", "query", "style"),
    [
        ([], [], "none"),
        (["Authorization"], [], "bearer"),
        (["x-api-key"], [], "api-key-header"),
        ([], ["api_key"], "api-key-query"),
        (["cookie"], [], "cookie"),
        (["referer"], [], "none"),
    ],
)
def test_auth_style(headers: list[str], query: list[str], style: str) -> None:
    assert d.auth_style(headers, query) == style


@pytest.mark.parametrize(
    ("title", "text", "expected"),
    [
        ("Just a moment...", "Checking your browser before accessing", True),
        ("Attention Required! | Cloudflare", "Sorry, you have been blocked", True),
        ("Access Denied", "You don't have permission", True),
        ("Book Finder", "Search books. Next page.", False),
        ("CAPTCHA tutorial", "x" * 5000, False),  # a long real page mentioning captcha
    ],
)
def test_bot_challenge(title: str, text: str, expected: bool) -> None:
    assert d.is_bot_challenge(title, text) is expected


def test_login_wall() -> None:
    assert d.looks_like_login_wall("https://e.com/", has_password_field=True)
    assert d.looks_like_login_wall("https://e.com/login?next=/", has_password_field=False)
    assert not d.looks_like_login_wall("https://e.com/blog/logins-explained", False)
