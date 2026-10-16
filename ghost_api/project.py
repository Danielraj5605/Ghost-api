"""A Ghost API project: a `ghost-api.toml` file and the folder it sits in.

`ghost-api init` writes one. Commands run anywhere inside that folder (or below it) then use
the project's database, output folder and defaults, wherever they are started from:

    [project]
    database = ".ghost-api/ghost_api.db"   # relative to this file
    output = "output"

    [defaults]                              # any command option, by its name
    lang = "typescript"
    rate_limit = 0.5

Per-site rules (a team can write down what is acceptable on each site):

    [project]
    require_terms = true            # every site needs terms_ok = true before the first run

    [sites."example.com"]
    terms_ok = true                 # someone checked the site's terms allow this
    deny_paths = ["/admin", "/api/private"]   # never requested, by the browser or directly
    max_direct_calls = 30           # endpoint tests and fetches per run
    agent = false                   # no AI explores this site (Ghost API's or an assistant)

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
class SitePolicy:
    terms_ok: bool = False
    deny_paths: tuple[str, ...] = ()
    max_direct_calls: int | None = None
    agent: bool = True

    def denies(self, path: str) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.deny_paths)


@dataclass(frozen=True)
class Project:
    root: Path
    database: Path
    output: Path
    defaults: dict[str, Any] = field(default_factory=dict)
    sites: dict[str, SitePolicy] = field(default_factory=dict)
    require_terms: bool = False

    def policy(self, site: str) -> SitePolicy:
        """The rules for a site: its own entry, else its host's, else none."""
        return self.sites.get(site) or self.sites.get(site.split("/")[0]) or SitePolicy()

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
    sites = data.get("sites", {})
    if not isinstance(sites, dict):
        raise ProjectError(f"{path}: [sites] must be a table of sites")
    return Project(
        root=root,
        database=root / str(settings.get("database", f"{STATE_FOLDER}/ghost_api.db")),
        output=root / str(settings.get("output", "output")),
        defaults=dict(defaults),
        sites={name: _policy(path, name, rules) for name, rules in sites.items()},
        require_terms=bool(settings.get("require_terms", False)),
    )


def _policy(path: Path, name: str, rules: Any) -> SitePolicy:
    if not isinstance(rules, dict):
        raise ProjectError(f'{path}: [sites."{name}"] must be a table')
    deny = rules.get("deny_paths", [])
    if not isinstance(deny, list) or not all(isinstance(p, str) and p.startswith("/")
                                             for p in deny):  # fmt: skip
        raise ProjectError(f'{path}: deny_paths for "{name}" must be paths like "/admin"')
    limit = rules.get("max_direct_calls")
    return SitePolicy(
        terms_ok=bool(rules.get("terms_ok", False)),
        deny_paths=tuple(deny),
        max_direct_calls=int(limit) if limit is not None else None,
        agent=bool(rules.get("agent", True)),
    )


def site_policy(site: str) -> tuple[SitePolicy, bool]:
    """(the site's rules, whether the project requires terms_ok), from ghost-api.toml."""
    project = find_project()
    if project is None:
        return SitePolicy(), False
    return project.policy(site), project.require_terms


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
