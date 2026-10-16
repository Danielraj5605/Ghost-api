"""Generator tests: naming, models, spec rules, rendering, and running generated code."""

import importlib
import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ghost_api.analysis.analyzer import analyze
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.models import CapturedRequest
from ghost_api.generator.models_gen import ModelBuilder
from ghost_api.generator.naming import (
    EndpointNames,
    NamingPlan,
    apply_plan,
    default_client_name,
    default_method_name,
    pascal,
    snake,
)
from ghost_api.generator.python_gen import GenerationError, check, render, write
from ghost_api.generator.spec import build_spec, is_user_input
from tests.fake_secrets import API_KEY, bearer, fake_value

HOST = "shop.test"
START = f"https://{HOST}/"


def captured(url: str, body: Any, method: str = "GET", req_body: Any = None) -> CapturedRequest:
    return CapturedRequest(
        method,
        url,
        200,
        "fetch",
        {"accept": "application/json"},
        json.dumps(req_body) if req_body is not None else None,
        "application/json" if req_body is not None else None,
        "application/json",
        json.dumps(body),
    )


def item_page(page: int) -> dict[str, Any]:
    return {
        "page": page,
        "has_next": page < 3,
        "items": [
            {"id": page * 10 + i, "itemName": f"n{i}", "class": "a", "json": 1} for i in range(2)
        ],
    }


REQUESTS = (
    [
        captured(f"https://{HOST}/api/items?page={p}&lang=en&api_key=[REDACTED]", item_page(p))
        for p in (1, 2)
    ]
    + [
        captured(
            f"https://{HOST}/api/items/{i}",
            {"id": i, "itemName": "x", "tags": ["a"], "maker": {"name": "m"}, "note": None},
        )
        for i in (11, 12)
    ]
    + [
        captured(
            f"https://{HOST}/api/search",
            {"hits": []},
            "POST",
            {"query": q, "page": 0, "filters": ["x"], "sort": s},
        )
        for q, s in (("dune", "date"), ("foundation", "relevance"))
    ]
)


def endpoints() -> list[Endpoint]:
    eps = analyze(REQUESTS)
    for ep in eps:
        ep.replayable = True
        if ep.path_template == "/api/items":
            ep.pagination = {
                "style": "page",
                "param": "page",
                "location": "query",
                "size_param": None,
                "confirmed": True,
            }
    return eps


def spec_for(eps: list[Endpoint] | None = None, plan: NamingPlan | None = None) -> Any:
    eps = eps if eps is not None else endpoints()
    cls, names = apply_plan(plan, eps, HOST)
    return build_spec(HOST, START, eps, REQUESTS, cls, names)


# --- naming ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("nbHits", "nb_hits"),
        ("x-algolia-agent", "x_algolia_agent"),
        ("2fa", "n_2fa"),
        ("class", "class_"),
        ("self", "self_"),
        ("", "value"),
    ],
)
def test_snake(raw: str, expected: str) -> None:
    assert snake(raw) == expected


def test_pascal_and_default_names() -> None:
    assert pascal("quotes_response") == "QuotesResponse"
    assert default_client_name("www.quotes.toscrape.com") == "QuotesToscrapeClient"
    assert default_client_name("127.0.0.1") == "LocalClient"
    assert default_client_name("localhost") == "LocalClient"
    eps = {e.path_template: e for e in endpoints()}
    assert default_method_name(eps["/api/items"]) == "get_items"
    assert default_method_name(eps["/api/items/{id}"]) == "get_item"
    assert default_method_name(Endpoint("POST", "x.algolia.net", "/1/indexes/{index}/query")) == (
        "post_indexes_query"
    )


def test_llm_names_are_used_when_valid_and_ignored_when_not() -> None:
    eps = endpoints()
    by_path = {e.path_template: e for e in eps}
    plan = NamingPlan(
        client_class="Shop",
        endpoints=[
            EndpointNames(
                key=by_path["/api/items"].key,
                method_name="listItems",
                response_model="ItemPage",
                summary="A page of items.",
            ),
            EndpointNames(
                key=by_path["/api/items/{id}"].key,
                method_name="list items!",
                response_model="not valid!",
                summary="",
            ),
        ],
    )
    cls, names = apply_plan(plan, eps, HOST)
    assert cls == "ShopClient"
    assert names[by_path["/api/items"].key].method_name == "list_items"
    assert names[by_path["/api/items"].key].response_model == "ItemPage"
    second = names[by_path["/api/items/{id}"].key]
    assert second.method_name == "list_items2"  # sanitized, then made unique
    assert second.response_model == "ItemResponse"  # invalid -> deterministic


# --- models ---


def test_models_nested_aliases_optional_and_dedupe() -> None:
    builder = ModelBuilder()
    schema = analyze([REQUESTS[2], REQUESTS[3]])[0].response_schema
    root = builder.root_type(schema, "ItemResponse")
    models = {m.name: m for m in builder.models}
    assert root == "ItemResponse"
    fields = {f.name: f for f in models["ItemResponse"].fields}
    assert fields["item_name"].alias == "itemName"
    assert fields["maker"].type == "Maker"
    assert fields["tags"].type == "list[str]"
    assert fields["note"].type == "Any" and fields["note"].optional  # only ever null


def test_reserved_and_keyword_field_names() -> None:
    builder = ModelBuilder()
    builder.root_type(analyze(REQUESTS[:2])[0].response_schema, "Page")
    item = next(m for m in builder.models if m.name == "Item")
    names = {f.name: f.alias for f in item.fields}
    assert names["class_"] == "class" and names["json_"] == "json"


def test_identical_structures_share_a_class() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            "b": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
        },
        "required": ["a", "b"],
    }
    builder = ModelBuilder()
    builder.root_type(schema, "Root")
    root = next(m for m in builder.models if m.name == "Root")
    assert [f.type for f in root.fields] == ["A", "A"]


# --- spec rules ---


def test_parameter_rules() -> None:
    spec = spec_for()
    methods = {m.name: m for m in spec.methods}
    items = methods["get_items"]
    params = {p.name: p for p in items.params}
    assert params["page"].required is False and params["page"].default == "1"
    assert params["lang"].default == "'en'"  # constant -> default
    assert "api_key" not in params  # secret -> credential
    assert items.credentials == ["api_key"]
    assert spec.credentials[0].env == "SHOP_TEST_API_KEY"
    assert items.page_param is not None

    search = {p.name: p for p in methods["post_search"].params}
    assert search["query"].required  # user input
    assert not search["sort"].required and search["sort"].default == "'date'"  # setting
    assert search["filters"].mutable_default and search["filters"].default_literal == "None"
    assert search["filters"].value_expr == "filters if filters is not None else ['x']"

    detail = methods["get_item"]
    assert [p.name for p in detail.required] == ["id"]
    assert detail.path == "/api/items/{id}"


@pytest.mark.parametrize(
    ("name", "examples", "expected"),
    [
        ("q", ["a"], True),
        ("userId", ["7"], True),
        ("sort", ["date"], False),
        ("hitsPerPage", ["20", "30"], False),
        ("ref", ["550e8400-e29b-41d4-a716-446655440000"], True),
    ],
)
def test_is_user_input(name: str, examples: list[str], expected: bool) -> None:
    assert is_user_input(name, examples) is expected


def test_unsafe_and_unreplayable_endpoints_are_skipped() -> None:
    eps = endpoints() + [
        Endpoint("DELETE", HOST, "/api/items/{id}"),
        Endpoint("GET", HOST, "/api/signed", replayable=False),
    ]
    spec = spec_for(eps)
    assert any("DELETE" in s and "change data" in s for s in spec.skipped)
    assert any("/api/signed" in s and "outside the browser" in s for s in spec.skipped)
    assert all(m.http_method in ("GET", "POST") for m in spec.methods)


# --- rendering and checks ---


def test_rendered_files_compile_and_contain_no_secrets() -> None:
    files = render(spec_for())
    check(files)
    client = next(
        src
        for name, src in files.items()
        if name.endswith("_client.py") and not name.startswith("test_")
    )
    assert "class ShopTestClient" in client and 'os.environ.get("SHOP_TEST_API_KEY")' in client


@pytest.mark.parametrize(
    "bad",
    ["x = '[REDACTED]'", f"token = '{bearer()}'", "def broken(:"],
)
def test_check_rejects_bad_output(bad: str) -> None:
    with pytest.raises(GenerationError):
        check({"x.py": bad})


# --- running the generated client ---


def fake_shop(request: httpx.Request) -> httpx.Response:
    url = urlsplit(str(request.url))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    if url.path == "/api/items":
        if q.get("api_key") != API_KEY:
            return httpx.Response(401, json={"error": "key"})
        page = int(q.get("page", "1"))
        if page > 3:
            return httpx.Response(200, json={"page": page, "has_next": False, "items": []})
        return httpx.Response(200, json=item_page(page))
    if url.path.startswith("/api/items/"):
        return httpx.Response(
            200,
            json={
                "id": int(url.path.rsplit("/", 1)[1]),
                "itemName": "x",
                "tags": [],
                "maker": {"name": "m"},
                "note": "hi",
            },
        )
    if url.path == "/api/search":
        body = json.loads(request.content)
        return httpx.Response(200, json={"hits": [body]})
    return httpx.Response(404, json={})


@pytest.fixture
def generated(tmp_path: Path) -> Iterator[ModuleType]:
    result = write(spec_for(), tmp_path)
    sys.path.insert(0, str(tmp_path))
    sys.modules.pop("models", None)
    try:
        yield importlib.import_module(result.module)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop(result.module, None)
        sys.modules.pop("models", None)


def client(module: ModuleType, **kw: Any) -> Any:
    return module.ShopTestClient(
        http_client=httpx.Client(transport=httpx.MockTransport(fake_shop)),
        max_requests_per_second=0,
        **kw,
    )


def test_generated_client_returns_typed_models(generated: ModuleType) -> None:
    api = client(generated, api_key=API_KEY)
    page = api.get_items()
    assert page.items[0].item_name == "n0" and page.items[0].class_ == "a"
    item = api.get_item(id=12)
    assert item.maker.name == "m" and item.note == "hi"
    result = api.post_search(query="dune")
    assert result.hits[0] == {"query": "dune", "page": 0, "filters": ["x"], "sort": "date"}


def test_generated_iterator_walks_pages(generated: ModuleType) -> None:
    api = client(generated, api_key=API_KEY)
    pages = list(api.iter_get_items())
    assert [p.page for p in pages] == [1, 2, 3]  # stops on has_next=False
    assert len(list(api.iter_get_items(max_pages=2))) == 2


def test_generated_credentials(generated: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOP_TEST_API_KEY", raising=False)
    with pytest.raises(generated.ShopTestError, match="SHOP_TEST_API_KEY"):
        client(generated).get_items()
    monkeypatch.setenv("SHOP_TEST_API_KEY", API_KEY)
    assert client(generated).get_items().page == 1


def test_generated_errors_and_retries(generated: ModuleType) -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(401, json={"error": "nope"})

    api = generated.ShopTestClient(
        api_key=fake_value("wrong"),
        max_requests_per_second=0,
        http_client=httpx.Client(transport=httpx.MockTransport(flaky)),
    )
    with pytest.raises(generated.ShopTestError) as err:
        api.get_items()
    assert err.value.status == 401 and err.value.body == {"error": "nope"}
    assert calls["n"] == 2  # retried the 503 once, then failed on the 401


def test_generated_errors_are_typed(generated: ModuleType) -> None:
    def status(code: int) -> Any:
        return generated.ShopTestClient(
            api_key="k",
            max_requests_per_second=0,
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda _r: httpx.Response(code, json={}))
            ),
        )

    for code, name in [
        (401, "ShopTestAuthError"),
        (403, "ShopTestAuthError"),
        (404, "ShopTestNotFoundError"),
        (429, "ShopTestRateLimitError"),
        (503, "ShopTestServerError"),
    ]:
        with pytest.raises(getattr(generated, name)) as err:
            status(code).get_items()
        assert isinstance(err.value, generated.ShopTestError) and err.value.status == code


def test_generated_async_client_matches_the_sync_one(generated: ModuleType) -> None:
    import asyncio

    async def main() -> tuple[Any, list[int]]:
        async with generated.AsyncShopTestClient(
            api_key=API_KEY,
            max_requests_per_second=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake_shop)),
        ) as api:
            item = await api.get_item(id=12)
            pages = [p.page async for p in api.iter_get_items()]
        return item, pages

    item, pages = asyncio.run(main())
    assert item.maker.name == "m" and pages == [1, 2, 3]


@pytest.mark.skipif(importlib.util.find_spec("mypy") is None, reason="mypy not installed")
def test_generated_python_passes_mypy_strict(tmp_path: Path) -> None:
    import subprocess

    result = write(spec_for(), tmp_path)
    loose = [result.client_path.name, "models.py", result.tests_path.name]
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--ignore-missing-imports",
         "--no-incremental", *loose],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_generate_client_end_to_end_with_llm_names(tmp_path: Path) -> None:
    from ghost_api.generator import generate_client
    from ghost_api.llm.gateway import LLMGateway
    from ghost_api.llm.providers import Provider
    from ghost_api.memory.store import Store

    eps = endpoints()
    by_path = {e.path_template: e for e in eps}
    plan = {
        "client_class": "ShopClient",
        "endpoints": [
            {
                "key": by_path["/api/items"].key,
                "method_name": "list_items",
                "response_model": "ItemPage",
                "summary": "One page of items.",
            },
        ],
    }

    def llm(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan)}}]})

    gateway = LLMGateway(
        [Provider("fake", "https://llm.test/v1", "m", api_key="k")],
        client=httpx.Client(transport=httpx.MockTransport(llm)),
    )
    with Store(tmp_path / "t.db") as store:
        run_id = store.create_run(HOST, START, "goal")
        for req in REQUESTS:
            store.add_request(run_id, req)
        store.save_endpoints(HOST, run_id, eps)
        result = generate_client(store, HOST, tmp_path / "out", llm=gateway)

    assert result.named_by_llm
    assert result.spec.class_name == "ShopClient"
    names = [m.name for m in result.spec.methods]
    assert "list_items" in names and "get_item" in names  # LLM name + fallback
    assert result.client.client_path.exists()
    assert "One page of items." in result.client.client_path.read_text(encoding="utf-8")


def test_cursor_parameters_are_text_even_when_they_look_like_numbers() -> None:
    from ghost_api.analysis.analyzer import analyze
    from ghost_api.generator.naming import apply_plan
    from ghost_api.generator.spec import build_spec

    reqs = [
        CapturedRequest("GET", f"https://ex.com/api/feed?cursor={c}", 200, "fetch", {}, None,
                        None, "application/json",
                        json.dumps({"items": [{"id": c}], "next_cursor": str(int(c) + 6)}))
        for c in ("6", "12", "18")
    ]  # fmt: skip
    eps = analyze(reqs)
    eps[0].pagination = {"style": "cursor", "param": "cursor", "location": "query",
                         "confirmed": True}  # fmt: skip
    cls, names = apply_plan(None, eps, "ex.com")
    method = build_spec("ex.com", "https://ex.com/", eps, reqs, cls, names).methods[0]
    cursor = next(p for p in method.params if p.wire == "cursor")
    assert cursor.type == "str" and cursor.default is None
