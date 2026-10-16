"""Write docs/cli.md from the command line itself, so the reference never drifts.

python scripts/gen_cli_docs.py           # write docs/cli.md
python scripts/gen_cli_docs.py --check   # exit 1 if docs/cli.md is out of date (CI)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from ghost_api.cli import app

TARGET = Path(__file__).resolve().parent.parent / "docs" / "cli.md"


def _option_row(param: Any) -> str:
    names = ", ".join(f"`{o}`" for o in [*param.opts, *param.secondary_opts])
    help_text = (getattr(param, "help", None) or "").replace("|", "/").replace("\n", " ")
    default = param.default
    shown = ""
    if not param.is_flag and default not in (None, (), [], False):
        shown = f" (default: `{default}`)"
    return f"| {names} | {help_text}{shown} |"


def _command(name: str, command: Any, depth: int) -> list[str]:
    lines = [f"{'#' * depth} `ghost-api {name}`", ""]
    doc = (command.help or "").strip()
    if doc:
        lines += [doc.replace("\b\n", ""), ""]
    # Typer bundles its own Click, so parameters are told apart by their kind.
    args = [p for p in command.params if p.param_type_name == "argument"]
    options = [p for p in command.params if p.param_type_name == "option" and p.name != "help"]
    if args:
        usage = " ".join(a.human_readable_name.upper() for a in args)
        lines += [f"```\nghost-api {name} {usage} [OPTIONS]\n```", ""]
    if options:
        lines += ["| Option | What it does |", "|---|---|", *(_option_row(o) for o in options), ""]
    return lines


def render() -> str:
    root: Any = typer.main.get_command(app)
    lines = [
        "# Command reference",
        "",
        "Generated from the command line (`python scripts/gen_cli_docs.py`). Every command",
        "also accepts `--json` (result as JSON on stdout) and `--plain` (line-by-line output),",
        "anywhere on the line. Exit codes: 0 done, 1 failed, 2 wrong usage or setup,",
        "3 not allowed by the site, 130 stopped with Ctrl+C.",
        "",
    ]
    for name in sorted(root.commands):
        command = root.commands[name]
        if hasattr(command, "commands"):  # a group, like `watch`
            lines += [f"## `ghost-api {name}`", "", (command.help or "").strip(), ""]
            for sub in sorted(command.commands):
                lines += _command(f"{name} {sub}", command.commands[sub], 3)
        else:
            lines += _command(name, command, 2)
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print("docs/cli.md is out of date: run python scripts/gen_cli_docs.py")
            sys.exit(1)
        print("docs/cli.md is up to date.")
    else:
        TARGET.write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote {TARGET}")
