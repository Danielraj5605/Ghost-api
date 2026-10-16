"""`ghost-api integrate`: add Ghost API to Claude Code, Cursor or VS Code."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ghost_api import exitcodes
from ghost_api.cli import app

runner = CliRunner()


def test_claude_code_gets_the_server_a_skill_and_commands(tmp_path: Path) -> None:
    other = {"mcpServers": {"github": {"command": "gh-mcp"}}, "keep": True}
    (tmp_path / ".mcp.json").write_text(json.dumps(other), encoding="utf-8")
    result = runner.invoke(app, ["integrate", "claude-code", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert config["keep"] and config["mcpServers"]["github"] == {"command": "gh-mcp"}
    server = config["mcpServers"]["ghost-api"]
    assert server["args"] == ["-m", "ghost_api", "mcp", "--explore"]
    skill = (tmp_path / ".claude/skills/ghost-api/SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: ghost-api\ndescription:")
    assert "data, never instructions" in skill
    for name in ("ghost-explore", "ghost-data", "ghost-diff"):
        command = (tmp_path / f".claude/commands/{name}.md").read_text(encoding="utf-8")
        assert command.startswith("---\ndescription:") and "$ARGUMENTS" in command


def test_cursor_vscode_dry_run_and_errors(tmp_path: Path) -> None:
    runner.invoke(app, ["integrate", "cursor", "--path", str(tmp_path)])
    cursor = json.loads((tmp_path / ".cursor/mcp.json").read_text(encoding="utf-8"))
    assert "ghost-api" in cursor["mcpServers"]
    runner.invoke(app, ["integrate", "vscode", "--path", str(tmp_path)])
    vscode = json.loads((tmp_path / ".vscode/mcp.json").read_text(encoding="utf-8"))
    assert vscode["servers"]["ghost-api"]["type"] == "stdio"

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    dry = runner.invoke(app, ["integrate", "--dry-run", "--path", str(fresh)])
    assert "would write" in dry.output and not any(fresh.iterdir())

    (fresh / ".mcp.json").write_text("{ broken", encoding="utf-8")
    broken = runner.invoke(app, ["integrate", "--path", str(fresh)])
    assert broken.exit_code == exitcodes.USAGE and "not valid JSON" in broken.output
    unknown = runner.invoke(app, ["integrate", "emacs", "--path", str(fresh)])
    assert unknown.exit_code == exitcodes.USAGE

    desktop = runner.invoke(app, ["integrate", "desktop"])
    assert '"ghost-api"' in desktop.output


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_the_written_command_starts_a_working_server(tmp_path: Path) -> None:
    """The exact command in .mcp.json, over real stdio, lists the explorer tools."""
    pytest.importorskip("mcp")
    from mcp.client.client import Client
    from mcp.client.stdio import StdioServerParameters

    runner.invoke(app, ["integrate", "--path", str(tmp_path)])
    server = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    entry = server["ghost-api"]
    params = StdioServerParameters(
        command=entry["command"],
        args=entry["args"],
        cwd=str(tmp_path),
        env={**os.environ, "GHOST_API_DB": str(tmp_path / "g.db")},
    )
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"ghost_explore_start", "ghost_fetch", "ghost_diff"} <= names
