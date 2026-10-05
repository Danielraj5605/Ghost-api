import pytest

from ghost_api.analysis.url_grouping import _singular, group_urls, infer_type, looks_like_id


def templates(*urls: str, method: str = "GET") -> list[str]:
    return [m.template for m in group_urls([(method, u) for u in urls])]


@pytest.mark.parametrize(
    "seg",
    [
        "123",
        "550e8400-e29b-41d4-a716-446655440000",
        "5f2b9c1e8a4d3b6c7e9f0a1b",
        "2024-05-01",
        "AbC123xyz456DEF789ghi012",
    ],
)
def test_looks_like_id(seg: str) -> None:
    assert looks_like_id(seg)


@pytest.mark.parametrize("seg", ["books", "v2", "api", "albert-einstein", "deadbeef"])
def test_not_id(seg: str) -> None:
    assert not looks_like_id(seg)


def test_numeric_ids_collapse() -> None:
    out = templates(
        "https://ex.com/api/books/1", "https://ex.com/api/books/2", "https://ex.com/api/books/3"
    )
    assert out == ["/api/books/{id}"] * 3


def test_single_sample_numeric_id_is_still_a_param() -> None:
    assert templates("https://ex.com/api/books/42") == ["/api/books/{id}"]


def test_nested_params_are_named_after_parent() -> None:
    out = templates("https://ex.com/users/7/posts/19", "https://ex.com/users/8/posts/20")
    assert out == ["/users/{user_id}/posts/{id}"] * 2


def test_names_come_from_each_templates_own_prefix() -> None:
    out = templates("https://ex.com/users/7/posts/19", "https://ex.com/teams/3/posts/2")
    assert out == ["/users/{user_id}/posts/{id}", "/teams/{team_id}/posts/{id}"]


def test_plain_word_siblings_stay_separate_endpoints() -> None:
    out = templates(
        "https://ex.com/api/quotes", "https://ex.com/api/authors", "https://ex.com/api/tags"
    )
    assert out == ["/api/quotes", "/api/authors", "/api/tags"]


def test_slugs_collapse() -> None:
    out = templates("https://ex.com/author/albert-einstein", "https://ex.com/author/jane-austen")
    assert out == ["/author/{author}"] * 2


def test_id_and_word_in_same_position_do_not_interfere() -> None:
    out = templates("https://ex.com/api/quotes/1", "https://ex.com/api/authors/me")
    assert out == ["/api/quotes/{id}", "/api/authors/me"]


def test_api_versions_stay_literal() -> None:
    out = templates("https://ex.com/api/v1/books", "https://ex.com/api/v2/books")
    assert out == ["/api/v1/books", "/api/v2/books"]


def test_high_cardinality_words_collapse() -> None:
    words = ["love", "life", "humor", "books", "truth", "inspirational"]
    out = templates(*(f"https://ex.com/tag/{w}" for w in words))
    assert set(out) == {"/tag/{tag}"}


def test_methods_and_hosts_are_kept_apart() -> None:
    matches = group_urls(
        [
            ("GET", "https://ex.com/api/items/1"),
            ("DELETE", "https://ex.com/api/items/1"),
            ("GET", "https://api.ex.com/api/items/1"),
        ]
    )
    assert {(m.method, m.host, m.template) for m in matches} == {
        ("GET", "ex.com", "/api/items/{id}"),
        ("DELETE", "ex.com", "/api/items/{id}"),
        ("GET", "api.ex.com", "/api/items/{id}"),
    }


def test_param_values_are_reported() -> None:
    (m,) = group_urls([("GET", "https://ex.com/users/7/posts/19")])
    assert m.param_names == ("user_id", "id")
    assert m.param_values == ("7", "19")


def test_leading_number_is_an_api_version() -> None:
    out = templates(
        "https://x.algolia.net/1/indexes/Item_dev/query",
        "https://x.algolia.net/1/indexes/Item_dev_sort_date/query",
    )
    assert out == ["/1/indexes/{index}/query"] * 2
    assert templates("https://ex.com/42") == ["/{id}"]  # alone, it's still an ID


def test_root_and_trailing_slash() -> None:
    assert templates("https://ex.com/", "https://ex.com") == ["/", "/"]
    assert templates("https://ex.com/api/items/") == ["/api/items/"]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["1", "2"], "integer"),
        (["1.5", "2"], "number"),
        (["true", "False"], "boolean"),
        (["a", "1"], "string"),
        ([], "string"),
    ],
)
def test_infer_type(values: list[str], expected: str) -> None:
    assert infer_type(values) == expected


@pytest.mark.parametrize(
    ("word", "singular"),
    [("books", "book"), ("categories", "category"), ("indexes", "index"),
     ("matches", "match"), ("addresses", "address"), ("news", "news"), ("status", "status")],
)  # fmt: skip
def test_singular(word: str, singular: str) -> None:
    assert _singular(word) == singular
