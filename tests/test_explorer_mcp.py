"""Phase A: an assistant explores a site through `ghost-api mcp --explore` (no LLM key)."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def body(result: Any) -> Any:
    text = result.content[0].text
    try:
        return json.loads(text)
    except ValueError:
        return text


@pytest.mark.anyio
async def test_an_assistant_maps_a_site_end_to_end(tmp_path: Path) -> None:
    from mcp.client.client import Client

    from evals.demo_shop import serve
    from ghost_api.serve.explorer_mcp import build_server

    shop = serve(0)
    url = f"http://127.0.0.1:{shop.server_address[1]}/"
    db, out = tmp_path / "g.db", tmp_path / "out"
    try:
        async with Client(build_server(db, out)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {
                "ghost_explore_start",
                "ghost_click",
                "ghost_explore_finish",
                "ghost_fetch",
                "ghost_diff",
            } <= names

            started = body(
                await client.call_tool(
                    "ghost_explore_start", {"url": url, "goal": "list and search products"}
                )
            )
            sid, page = started["session"], started["page"]
            assert page.startswith("<<<PAGE") and "data, not instructions" in page
            assert {e["path"] for e in started["endpoints"]} >= {"/api/products"}

            search = int(re.search(r"\[(\d+)\] input:search", page).group(1))  # type: ignore[union-attr]
            typed = body(
                await client.call_tool(
                    "ghost_type",
                    {"session": sid, "element_id": search, "text": "games", "why": "find search"},
                )
            )
            assert "GET /api/search" in typed["new_endpoints"]

            # The rules hold whoever decides: no leaving the site.
            away = body(
                await client.call_tool(
                    "ghost_navigate", {"session": sid, "url": "https://elsewhere.example/"}
                )
            )
            assert away["result"].startswith("rejected")

            tested = body(
                await client.call_tool(
                    "ghost_test_endpoint",
                    {"session": sid, "endpoint": "GET /api/products", "params": {"page": "2"}},
                )
            )
            assert "200" in tested["result"]

            done = body(
                await client.call_tool(
                    "ghost_explore_finish", {"session": sid, "goal_covered": True}
                )
            )
            assert done["status"] == "goal-covered"
            assert done["client"]["files"] and Path(done["report"]).exists()
            assert done["network"]["llm_calls"] == 0  # the assistant was the agent

            closed = await client.call_tool("ghost_page", {"session": sid})
            assert closed.is_error and "no open exploration" in closed.content[0].text  # type: ignore[union-attr]

            # Library tools on what was just explored.
            assert body(await client.call_tool("ghost_sites", {}))["sites"] == ["127.0.0.1"]
            ops = {
                o["name"]
                for o in body(await client.call_tool("ghost_operations", {"site": "127.0.0.1"}))
            }
            assert "search" in " ".join(ops)
            rows = body(
                await client.call_tool(
                    "ghost_fetch", {"site": "127.0.0.1", "operation": "get_products", "max_rows": 5}
                )
            )
            assert rows["rows"] == 5
            diff = body(await client.call_tool("ghost_diff", {"site": "127.0.0.1"}))
            assert diff["changes"] == [] and diff["version"] == "1.0.0"
    finally:
        shop.shutdown()


@pytest.mark.anyio
async def test_robots_refusal_is_a_clear_tool_error(tmp_path: Path) -> None:
    from mcp.client.client import Client

    from evals.fixtures import serve
    from ghost_api.serve.explorer_mcp import build_server

    site = serve("robots-disallow")
    try:
        async with Client(build_server(tmp_path / "g.db", tmp_path / "out")) as client:
            result = await client.call_tool(
                "ghost_explore_start", {"url": site.url + "api/private/users", "goal": "x"}
            )
            assert result.is_error and "robots.txt" in result.content[0].text  # type: ignore[union-attr]
        assert not site.hit("/api/private")
    finally:
        site.shutdown()
