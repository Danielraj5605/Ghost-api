"""A Ghost API project: a `ghost-api.toml` file and the folder it sits in.

`ghost-api init` writes one. Commands run anywhere inside that folder (or below it) then use
the project's database, output folder and defaults, wherever they are started from:

    [project]
    database = ".ghost-api/ghost_api.db"   # relative to this file
    output = "output"

    [defaults]                              # any command option, by its name
    lang = "typescript"
    rate_limit = 0.5

Without a project file, Ghost API behaves as before: `ghost_api.db` and `output/` in the
current folder. `GHOST_API_DB` and command-line options always win.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_FILE = "ghost-api.toml"
STATE_FOLDER = ".ghost-api"


class ProjectError(Exception):
    """The project file can't be read."""


@dataclass(frozen=True)
class Project:
    root: Path
    database: Path
    output: Path
    defaults: dict[str, Any] = field(default_factory=dict)

    @property
    def file(self) -> Path:
        return self.root / PROJECT_FILE


def find_project(start: Path | None = None) -> Project | None:
    """The nearest `ghost-api.toml` in `start` or a folder above it."""
    if os.getenv("GHOST_API_NO_PROJECT"):
        return None
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / PROJECT_FILE
        if candidate.is_file():
            return load_project(candidate)
    return None


def load_project(path: Path) -> Project:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectError(f"{path} can't be read: {exc}") from exc
    root = path.parent
    settings = data.get("project", {})
    defaults = data.get("defaults", {})
    if not isinstance(settings, dict) or not isinstance(defaults, dict):
        raise ProjectError(f"{path}: [project] and [defaults] must be tables")
    return Project(
        root=root,
        database=root / str(settings.get("database", f"{STATE_FOLDER}/ghost_api.db")),
        output=root / str(settings.get("output", "output")),
        defaults=dict(defaults),
    )


TEMPLATE = """\
# Ghost API project settings. Commands run in this folder (or below it) use them.
# Docs: https://github.com/Danielraj5605/Ghost-api#project-settings

[project]
database = ".ghost-api/ghost_api.db"  # runs, captured traffic, endpoints (keep private)
output = "output"                     # reports, clients, downloads

[defaults]
# Any command option, by its name (dashes or underscores). Command-line options win.
lang = "{lang}"
rate_limit = 1.0        # requests per second Ghost API sends to a site
# max_steps = 40        # agent actions per run
# max_llm_calls = 50
"""

GITIGNORE_LINES = (".env", ".ghost-api/", "output/")


def write_project(root: Path, lang: str = "python") -> Path:
    path = root / PROJECT_FILE
    path.write_text(TEMPLATE.format(lang=lang), encoding="utf-8")
    (root / STATE_FOLDER).mkdir(exist_ok=True)
    return path


def ensure_gitignore(root: Path) -> list[str]:
    """Keep keys and collected data out of git. Returns the lines that were added."""
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [line for line in GITIGNORE_LINES if line not in existing]
    if missing:
        text = "\n".join(existing).rstrip()
        block = "\n".join(["# Ghost API: keys and collected data", *missing])
        path.write_text(f"{text}\n\n{block}\n" if text else f"{block}\n", encoding="utf-8")
    return missing


def default_map(project: Project, command: Any) -> dict[str, Any]:
    """Click `default_map` for every (sub)command: the project's output folder and
    [defaults] for each command that has an option of that name."""
    values = {k.replace("-", "_"): v for k, v in project.defaults.items()}
    values.setdefault("out", str(project.output))

    def build(cmd: Any) -> dict[str, Any]:
        names = {p.name for p in getattr(cmd, "params", [])}
        result: dict[str, Any] = {k: v for k, v in values.items() if k in names}
        for name, sub in getattr(cmd, "commands", {}).items():
            nested = build(sub)
            if nested:
                result[name] = nested
        return result

    return build(command)
