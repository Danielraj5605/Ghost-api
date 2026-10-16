"""The command reference in docs/cli.md is generated from the CLI and must stay current."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_command_reference_is_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHOST_API_NO_PROJECT", "1")
    spec = importlib.util.spec_from_file_location("gen_cli_docs", ROOT / "scripts/gen_cli_docs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert module.render() == current, "run: python scripts/gen_cli_docs.py"
    for command in ("run", "import", "login", "diff", "mock", "init", "forget"):
        assert f"## `ghost-api {command}`" in current
