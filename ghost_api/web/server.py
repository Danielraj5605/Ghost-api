"""Placeholder until Phase 3: the dashboard server."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console


def serve(db: Path, out: Path, port: int, open_browser: bool, console: Console) -> None:
    console.print("The web dashboard is not built yet.")
