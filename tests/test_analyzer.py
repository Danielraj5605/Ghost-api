import json
from pathlib import Path
from typing import Any

from ghost_api.analysis.analyzer import analyze, classify_query_params
from ghost_api.analysis.schema import field_paths, top_level_fields, type_label
from ghost_api.capture.models import CapturedRequest
from ghost_api.memory.store import Store


def req(
    url: str,
    body: Any = None,
    method: str = "GET",
    status: int = 200,
    req_body: Any = None,
    truncated: bool = False,
) -> CapturedRequest:
    return CapturedRequest(
        method=method,
        url=url,
        status=status,
        resource_type="fetch",
        req_headers={},
        req_body=json.dumps(req_body) if req_body is not None else None,
        req_content_type="application/json" if req_body is not None else None,
        resp_content_type="application/json",
        resp_body=json.dumps(body) if body is not None else None,
        truncated=truncated,
    )


def quote_page(p: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "page": p,
        "has_next": p < 3,
        "quotes": [{"text": "t", "author": {"name": "a"}}],
    }
    if p == 1:
        body["tag"] = None  # only in one sample, so it must become optional
    return body


QUOTES = [req(f"https://quotes.example.com/api/quotes?page={p}", quote_page(p)) for p in (1, 2, 3)]


def test_paginated_endpoint_becomes_one_endpoint() -> None:
    (ep,) = analyze(QUOTES)
    assert ep.label == "GET /api/quotes"
    assert ep.sample_count == 3 and ep.response_samples == 3
    (page,) = ep.query_params
    assert (page.name, page.kind, page.type) == ("page", "required", "integer")
    assert page.examples == ["1", "2", "3"]


def test_schema_merges_samples_and_marks_optional_fields() -> None:
    (ep,) = analyze(QUOTES)
    schema = ep.response_schema
    assert schema is not None
    assert set(schema["required"]) == {"page", "has_next", "quotes"}
    assert "tag" in schema["properties"]
    assert schema["properties"]["quotes"]["items"]["properties"]["author"]["type"] == "object"


def test_query_param_kinds() -> None:
    params = {
        p.name: p
        for p in classify_query_params(
            [
                "https://ex.com/s?q=dune&lang=en&page=1&api_key=[REDACTED]",
                "https://ex.com/s?q=foundation&lang=en&api_key=[REDACTED]",
            ]
        )
    }
    assert params["q"].kind == "required"
    assert params["lang"].kind == "constant"
    assert params["page"].kind == "optional"
    assert params["api_key"].sensitive and params["api_key"].examples == []


def test_single_sample_param_is_not_constant() -> None:
    (p,) = classify_query_params(["https://ex.com/s?q=dune"])
    assert p.kind == "required"


def test_errors_and_truncated_bodies_do_not_pollute_schema() -> None:
    (ep,) = analyze(
        [
            req("https://ex.com/api/books/1", {"id": 1, "title": "x"}),
            req("https://ex.com/api/books/2", {"error": "not found"}, status=404),
            req("https://ex.com/api/books/3", {"id": 3, "title": "y"}, truncated=True),
        ]
    )
    assert ep.path_template == "/api/books/{id}"
    assert ep.statuses == [200, 404]
    assert ep.response_samples == 1
    assert ep.response_schema is not None
    assert "error" not in ep.response_schema["properties"]
    (pid,) = ep.path_params
    assert (pid.name, pid.type, pid.examples) == ("id", "integer", ["1", "2", "3"])


def test_request_body_schema() -> None:
    (ep,) = analyze(
        [req("https://ex.com/api/search", {"hits": []}, method="POST", req_body={"q": "dune"})]
    )
    assert ep.request_schema is not None
    assert ep.request_schema["properties"]["q"]["type"] == "string"
    assert ep.request_samples == 1


def test_empty_input() -> None:
    assert analyze([]) == []


def test_schema_summaries() -> None:
    (ep,) = analyze(QUOTES)
    assert top_level_fields(ep.response_schema) == "page, has_next, quotes[], tag"
    assert type_label({"type": "array", "items": {"type": "object"}}) == "array[object]"
    assert top_level_fields({"type": "array", "items": {"type": "string"}}) == "[] of string"
    assert top_level_fields(None) == "-"


def test_store_roundtrip_and_upsert(tmp_path: Path) -> None:
    with Store(tmp_path / "t.db") as store:
        run_id = store.create_run("example.com", "https://quotes.example.com", "g")
        endpoints = analyze(QUOTES)
        store.save_endpoints("example.com", run_id, endpoints)
        store.save_endpoints("example.com", run_id, endpoints)  # upsert, no duplicates
        loaded = store.load_endpoints("example.com")
    assert loaded == endpoints


def test_field_paths_show_nested_data() -> None:
    (ep,) = analyze(QUOTES)
    paths = field_paths(ep.response_schema)
    assert "quotes[].author.name" in paths
    assert "quotes[].text" in paths and "page" in paths
    assert field_paths({"type": "array", "items": {"type": "string"}}) == ["[]"]
    assert field_paths(None) == []
    assert len(field_paths(ep.response_schema, limit=2)) == 2
