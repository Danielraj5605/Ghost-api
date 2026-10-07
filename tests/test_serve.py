"""Use it from other tools (phase 5): `ghost-api serve` (REST) and `ghost-api mcp` (MCP)."""

import csv
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ghost_api.memory.store import Store
from ghost_api.runtime import SiteRuntime
from ghost_api.serve import ServedSite, coerce, load_sites, tool_name
from ghost_api.serve.rest import create_rest_app
from tests.test_runtime import fast_replayer, shop_db  # noqa: F401


@pytest.fixture
def served(shop_db: tuple[Path, str]) -> dict[str, ServedSite]:  # noqa: F811
    db, _ = shop_db
    with Store(db) as store:
        rt = SiteRuntime(store, "127.0.0.1", fast_replayer("127.0.0.1"))
    return {"127.0.0.1": ServedSite("127.0.0.1", rt, rt.operations())}


def test_names_and_inputs() -> None:
    class Op:
        name = "get_nearby_venues"

    assert tool_name("turftown.in/chennai", Op()) == "turftown_chennai_get_nearby_venues"  # type: ignore[arg-type]
    assert tool_name("www.google.com/maps", Op()).startswith("google_maps_")  # type: ignore[arg-type]
    assert coerce(["football", "cricket"]) == '["football", "cricket"]'
    assert coerce(True) == "true" and coerce(5) == "5"


def test_load_sites_skips_sites_with_nothing_to_call(shop_db: tuple[Path, str]) -> None:  # noqa: F811
    db, _ = shop_db
    sites = load_sites(db)
    assert list(sites) == ["127.0.0.1"]
    assert "get_products" in {op.name for op in sites["127.0.0.1"].operations}
    for s in sites.values():
        s.runtime.close()


def test_rest_api(served: dict[str, ServedSite]) -> None:
    client = TestClient(create_rest_app(served, {"testserver"}))
    index = client.get("/").json()
    ops = {o["name"]: o for o in index["sites"][0]["operations"]}
    assert ops["get_products"]["url"].endswith("/127.0.0.1/get_products")
    rows = client.get("/127.0.0.1/get_products", params={"per_page": "10", "pages": 5}).json()
    assert rows["rows"] == 30 and rows["pages"] == 3 and len({r["id"] for r in rows["data"]}) == 30
    text = client.get("/127.0.0.1/get_products", params={"per_page": "10", "format": "csv"})
    table = list(csv.DictReader(io.StringIO(text.content.decode("utf-8-sig"))))
    assert len(table) == 10 and text.headers["x-ghost-rows"] == "10"
    docs = client.get("/openapi.json").json()
    params = {p["name"] for p in docs["paths"]["/127.0.0.1/get_products"]["get"]["parameters"]}
    assert {"per_page", "page", "pages", "format"} <= params  # every input is documented
    assert client.get("/127.0.0.1/get_products", params={"format": "xml"}).status_code == 422
    evil = TestClient(create_rest_app(served, {"127.0.0.1:8900"}))
    assert evil.get("/").status_code == 403  # Host check (DNS rebinding)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_tools(served: dict[str, ServedSite]) -> None:
    pytest.importorskip("mcp")
    from mcp.client.client import Client

    from ghost_api.serve.mcp import build_server

    async with Client(build_server(served)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        tool = (
            tools["127_0_get_products"]
            if "127_0_get_products" in tools
            else next(t for name, t in tools.items() if name.endswith("get_products"))
        )
        assert {"per_page", "pages", "max_rows"} <= set(tool.input_schema["properties"])
        assert "read-only" in (tool.description or "")
        result = await client.call_tool(tool.name, {"per_page": 10, "pages": 5, "max_rows": 25})
        assert not result.is_error
        data = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert data["rows"] == 25 and data["operation"] == "get_products"
        missing = await client.call_tool("nope", {})
        assert missing.is_error and "Unknown tool" in missing.content[0].text  # type: ignore[union-attr]
