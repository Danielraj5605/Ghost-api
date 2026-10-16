"""Phase 4b: GraphQL operations become one typed method each."""

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from ghost_api.analysis.analyzer import analyze, endpoint_keys
from ghost_api.analysis.graphql import operation_of, split_template
from ghost_api.capture.models import CapturedRequest
from ghost_api.generator.naming import apply_plan
from ghost_api.generator.python_gen import write
from ghost_api.generator.spec import build_spec
from ghost_api.generator.ts_gen import render_typescript

GET_USER = "query GetUser($id: ID!) { user(id: $id) { id name } }"
LIST_POSTS = "query ListPosts($first: Int) { posts(first: $first) { id } }"


def gq(op: str | None, query: str, variables: dict[str, Any], reply: dict[str, Any]) -> Any:
    body: dict[str, Any] = {"query": query, "variables": variables}
    if op:
        body["operationName"] = op
    return CapturedRequest("POST", "https://api.ex.com/graphql", 200, "fetch",
                           {"content-type": "application/json"}, json.dumps(body),
                           "application/json", "application/json", json.dumps(reply))  # fmt: skip


def requests() -> list[CapturedRequest]:
    reqs = [gq("GetUser", GET_USER, {"id": str(i)},
               {"data": {"user": {"id": str(i), "name": "n"}}}) for i in range(3)]  # fmt: skip
    reqs += [gq(None, LIST_POSTS, {"first": n}, {"data": {"posts": [{"id": 1}]}})
             for n in (5, 10)]  # fmt: skip
    return reqs


def test_operations_are_named_from_the_body_or_the_query() -> None:
    assert operation_of("POST", "https://x/graphql", json.dumps({"query": LIST_POSTS})) == (
        "ListPosts"
    )
    url = "https://x/graphql?query=" + "%7B%20me%20%7D&operationName=Me"
    assert operation_of("GET", url, None) == "Me"
    assert operation_of("POST", "https://x/api/search", json.dumps({"q": "dune"})) is None
    assert operation_of("POST", "https://x/graphql", json.dumps([{"query": GET_USER}])) is None
    assert split_template("/graphql#GetUser") == ("/graphql", "GetUser")


def test_each_operation_is_its_own_endpoint() -> None:
    reqs = requests()
    labels = {e.label for e in analyze(reqs)}
    assert labels == {"POST /graphql#GetUser", "POST /graphql#ListPosts"}
    assert endpoint_keys(reqs)[0] == "POST api.ex.com/graphql#GetUser"


def spec() -> Any:
    reqs = requests()
    eps = analyze(reqs)
    cls, names = apply_plan(None, eps, "ex.com")
    return build_spec("ex.com", "https://ex.com/", eps, reqs, cls, names)


def test_variables_become_typed_arguments() -> None:
    methods = {m.name: m for m in spec().methods}
    get_user = methods["get_user"]
    assert get_user.path == "/graphql"
    assert [(p.name, p.location, p.required) for p in get_user.params] == [("id", "variable", True)]
    assert get_user.graphql == {"operationName": "GetUser", "query": GET_USER}
    first = methods["list_posts"].params[0]
    assert (first.name, first.type, first.required) == ("first", "int", False)


def test_the_generated_client_sends_the_operation(tmp_path: Path) -> None:
    result = write(spec(), tmp_path)
    sent: list[dict[str, Any]] = []

    def server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body["operationName"] == "GetUser":
            uid = body["variables"]["id"]
            return httpx.Response(200, json={"data": {"user": {"id": uid, "name": "n"}}})
        return httpx.Response(200, json={"data": {"posts": [{"id": 1}]}})

    sys.path.insert(0, str(tmp_path))
    sys.modules.pop("models", None)
    try:
        module = importlib.import_module(result.module)
        api = module.ExClient(http_client=httpx.Client(transport=httpx.MockTransport(server)),
                              max_requests_per_second=0)  # fmt: skip
        user = api.get_user("7")
        api.list_posts()
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop(result.module, None)
        sys.modules.pop("models", None)
    assert user.data.user.id == "7"
    assert sent[0] == {"operationName": "GetUser", "query": GET_USER, "variables": {"id": "7"}}
    assert sent[1]["variables"] == {"first": 5}  # the value seen working, by default


def test_the_typescript_client_sends_the_operation_too() -> None:
    source = next(src for name, src in render_typescript(spec()).items()
                  if name.endswith("_client.ts"))  # fmt: skip
    assert "async getUser(" in source and '"operationName": "GetUser"' in source
    assert "variables: compact({" in source
