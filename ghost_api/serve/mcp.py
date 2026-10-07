"""`ghost-api mcp`: stored sites as tools for AI assistants (Model Context Protocol, stdio).

Add it to Claude Code:  claude mcp add ghost-turftown -- ghost-api mcp turftown.in/chennai

Each callable operation becomes a tool ("turftown_chennai_get_nearby_venues") whose inputs
are the operation's inputs, plus `pages` and `max_rows`. Tools are read-only, one call at
a time per site, and return JSON rows with personal data hidden. Needs the `mcp` package
(`pip install "ghost-api[mcp]"`).
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server

from ghost_api import __version__
from ghost_api.probe.replay import ReplayRefused
from ghost_api.runtime import Operation, OperationError
from ghost_api.serve import ServedSite, coerce, tool_name

LIST_HINT = " A list: give one value or several separated by commas."
MAX_TEXT = 24_000  # keep answers small enough for an assistant's context
INSTRUCTIONS = (
    "These tools call websites' own (undocumented) APIs that Ghost API discovered. They only "
    "read data, slowly, and personal data in replies is replaced by '[hidden ...]' markers. "
    "Each tool's inputs show example values the website itself used; most are optional."
)


def build_server(sites: dict[str, ServedSite]) -> Server[Any]:
    tools: dict[str, tuple[ServedSite, Operation]] = {}
    for served in sites.values():
        for op in served.operations:
            tools[tool_name(served.site, op)] = (served, op)

    async def list_tools(
        ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[_tool(name, served, op) for name, (served, op) in tools.items()]
        )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        found = tools.get(params.name)
        if found is None:
            return _error(f"Unknown tool {params.name}. Use one of: {', '.join(tools)}")
        served, op = found
        args = dict(params.arguments or {})
        pages = int(args.pop("pages", 1) or 1)
        max_rows = min(int(args.pop("max_rows", 50) or 50), 500)
        inputs = {k: coerce(v) for k, v in args.items() if v is not None}
        try:
            result = await anyio.to_thread.run_sync(
                lambda: served.fetch(op, inputs, pages, max_rows)
            )
        except (OperationError, ReplayRefused) as exc:
            return _error(str(exc))
        if result["error"] and not result["rows"]:
            return _error(f"The site answered: {result['error']}")
        return types.CallToolResult(content=[types.TextContent(type="text", text=_fit(result))])

    return Server(
        "ghost-api",
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _tool(name: str, served: ServedSite, op: Operation) -> types.Tool:
    properties: dict[str, Any] = {}
    for item in op.inputs:
        hint = LIST_HINT if item.type == "list" else ""
        properties[item.name] = {
            "type": "string",
            "description": f"{item.location} {item.type} input; the site used "
            f"{item.example or 'nothing'}.{hint}",
        }
    properties["pages"] = {"type": "integer", "minimum": 1, "maximum": 20, "default": 1,
                           "description": "How many pages to follow."}  # fmt: skip
    properties["max_rows"] = {"type": "integer", "minimum": 1, "maximum": 500, "default": 50,
                              "description": "Stop after this many rows."}  # fmt: skip
    return types.Tool(
        name=name,
        description=f"{op.summary} (from {served.site}, via {op.label}; read-only).",
        input_schema={
            "type": "object",
            "properties": properties,
            "required": [i.name for i in op.inputs if i.required],
        },
    )


def _fit(result: dict[str, Any]) -> str:
    """JSON text under MAX_TEXT: drop rows from the end and say so."""
    data = result["data"]
    text = json.dumps(result, ensure_ascii=False, default=str)
    while len(text) > MAX_TEXT and data:
        data = data[: max(0, len(data) * 3 // 4)]
        text = json.dumps({**result, "data": data, "truncated_to_rows": len(data)},
                          ensure_ascii=False, default=str)  # fmt: skip
    return text[:MAX_TEXT]


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


async def run_stdio(sites: dict[str, ServedSite]) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(sites)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
