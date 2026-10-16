"""`ghost-api integrate`: add Ghost API to an AI assistant, for one project.

claude-code  .mcp.json (the MCP server), .claude/skills/ghost-api/SKILL.md (when and how
             to use it) and .claude/commands/ghost-*.md (/ghost-explore, /ghost-data,
             /ghost-diff)
cursor       .cursor/mcp.json
vscode       .vscode/mcp.json
desktop      prints the snippet for Claude Desktop's own settings (not written for you)

Existing config files are merged, never replaced: other MCP servers stay as they are.
The server is started with this Python (`python -m ghost_api mcp --explore`), which works
even where installed launchers are blocked.
"""

from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path
from typing import Any

SERVER_NAME = "ghost-api"
TARGETS = ("claude-code", "cursor", "vscode", "desktop")


class IntegrationError(Exception):
    """A config file Ghost API should not touch (unreadable, not JSON...)."""


def server_entry() -> dict[str, Any]:
    return {"command": sys.executable, "args": ["-m", "ghost_api", "mcp", "--explore"]}


def _merge_json(path: Path, key: str, entry: dict[str, Any]) -> str:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise IntegrationError(f"{path} is not valid JSON; fix it or remove it first") from exc
        if not isinstance(data, dict):
            raise IntegrationError(f"{path} is not a JSON object")
    servers = data.setdefault(key, {})
    if not isinstance(servers, dict):
        raise IntegrationError(f"{path}: '{key}' is not an object")
    servers[SERVER_NAME] = entry
    return json.dumps(data, indent=2) + "\n"


def _bundled(relative: str) -> str:
    package = resources.files("ghost_api.integrations") / "claude_code"
    return (package / relative).read_text(encoding="utf-8")


def plan(target: str, root: Path) -> dict[Path, str]:
    """The files `target` needs under `root`, with their new content (nothing written)."""
    if target == "claude-code":
        files = {root / ".mcp.json": _merge_json(root / ".mcp.json", "mcpServers",
                                                 server_entry())}  # fmt: skip
        files[root / ".claude" / "skills" / "ghost-api" / "SKILL.md"] = _bundled(
            "skills/ghost-api/SKILL.md"
        )
        for name in ("ghost-explore", "ghost-data", "ghost-diff"):
            files[root / ".claude" / "commands" / f"{name}.md"] = _bundled(f"commands/{name}.md")
        return files
    if target == "cursor":
        path = root / ".cursor" / "mcp.json"
        return {path: _merge_json(path, "mcpServers", server_entry())}
    if target == "vscode":
        path = root / ".vscode" / "mcp.json"
        return {path: _merge_json(path, "servers", {"type": "stdio", **server_entry()})}
    raise IntegrationError(f"unknown target {target!r}; choose from: {', '.join(TARGETS)}")


def desktop_snippet() -> str:
    return json.dumps({"mcpServers": {SERVER_NAME: server_entry()}}, indent=2)


def write(files: dict[Path, str]) -> list[Path]:
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return list(files)
