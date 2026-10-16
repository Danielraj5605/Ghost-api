"""`ghost-api adopt PATH`: find hand-written calls to undocumented APIs in a codebase.

Scans source files for HTTP calls with a literal URL (fetch, axios, requests, httpx,
urllib, aiohttp...), groups them by host and, for hosts Ghost API has explored, matches each
call to the generated client method that replaces it. The result is a replacement map a
developer (or an AI assistant) can apply; nothing is edited here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ghost_api.analysis.graphql import split_template
from ghost_api.generator import NothingToGenerate, site_spec
from ghost_api.memory.store import Store
from ghost_api.site import SiteAmbiguous, resolve_site

SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"}
SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "env", "dist", "build", "__pycache__", ".next",
    "out", "output", "coverage", ".tox", ".mypy_cache", ".ruff_cache", "site-packages",
}  # fmt: skip
MAX_FILE_BYTES = 1_000_000

# A call that sends a request, then the first string literal URL in its arguments.
CALL = re.compile(
    r"""(?P<call>
        \bfetch\s*\(
      | \baxios(?:\.(?P<axios>get|post|put|patch|delete))?\s*\(
      | \b(?:requests|httpx|session|client|aiohttp\.ClientSession\(\)|ky|got|superagent)
        \.(?P<verb>get|post|put|patch|delete|request)\s*\(
      | \burlopen\s*\(
      | \$\.(?:get|post|ajax|getJSON)\s*\(
    )\s*(?:["'`](?P<method_arg>GET|POST|PUT|PATCH|DELETE)["'`]\s*,\s*)?
    [fF]?["'`](?P<url>https?://[^"'`\s]+)["'`]""",
    re.VERBOSE,
)
METHOD_OPTION = re.compile(r"""method\s*[:=]\s*["'`](GET|POST|PUT|PATCH|DELETE)["'`]""", re.I)


@dataclass
class Call:
    file: str
    line: int
    method: str
    url: str
    code: str  # the line, trimmed
    replacement: str | None = None  # "client.get_quotes(page=2)"
    endpoint: str | None = None  # "GET /api/quotes"


@dataclass
class HostReport:
    host: str
    site: str | None  # the explored site that covers it, if any
    calls: list[Call] = field(default_factory=list)
    client_class: str | None = None
    next_step: str = ""


def scan(root: Path) -> list[Call]:
    """Every HTTP call with a literal http(s) URL under `root`."""
    calls: list[Call] = []
    for path in sorted(_source_files(root)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in CALL.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            window = text[match.end() : match.end() + 300]
            method = (
                match.group("method_arg")
                or (match.group("verb") or match.group("axios") or "").upper()
                or _method_option(window)
                or "GET"
            )
            if method == "REQUEST":
                method = _method_option(window) or "GET"
            code = text.splitlines()[line - 1].strip()[:160]
            calls.append(Call(str(path.relative_to(root)), line, method,
                              match.group("url").rstrip(").,;"), code))  # fmt: skip
    return calls


def _method_option(window: str) -> str | None:
    found = METHOD_OPTION.search(window.split(")", 1)[0])
    return found.group(1).upper() if found else None


def _source_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        small = path.is_file() and path.stat().st_size <= MAX_FILE_BYTES
        if path.suffix in SOURCE_SUFFIXES and small:
            files.append(path)
    return files


def adopt(root: Path, store: Store | None) -> list[HostReport]:
    """The calls grouped by host, matched to explored sites' client methods."""
    by_host: dict[str, list[Call]] = {}
    for call in scan(root):
        host = (urlsplit(call.url).hostname or "").lower()
        if host and host not in ("localhost", "127.0.0.1"):
            by_host.setdefault(host, []).append(call)
    known = sorted({r.domain for r in store.list_runs()}) if store is not None else []
    reports = []
    for host, calls in sorted(by_host.items(), key=lambda kv: -len(kv[1])):
        site = _site_for(host, calls, known)
        report = HostReport(host, site, calls)
        if site is None or store is None:
            origin = f"https://{host}"
            report.next_step = (
                f'ghost-api run {origin} --goal "..."  (or record it in your browser and '
                "ghost-api import FILE.har)"
            )
        else:
            _match(store, site, report)
        reports.append(report)
    return reports


def _site_for(host: str, calls: list[Call], known: list[str]) -> str | None:
    candidates = [s for s in known if s.split("/")[0] == host]
    if not candidates:
        return None
    try:
        return resolve_site(host, set(candidates))
    except SiteAmbiguous:  # several sections: the one most calls' paths start with
        paths = [urlsplit(c.url).path for c in calls]
        return max(candidates, key=lambda s: sum(p.startswith("/" + s.partition("/")[2])
                                                  for p in paths))  # fmt: skip


def _match(store: Store, site: str, report: HostReport) -> None:
    try:
        spec = site_spec(store, site)
    except NothingToGenerate:
        report.next_step = f"ghost-api refresh {site}  (nothing usable stored yet)"
        return
    report.client_class = spec.class_name
    patterns = []
    for method in spec.methods:
        path, _ = split_template(method.path)
        regex = "^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(path)) + "/?$"
        patterns.append((method, re.compile(regex)))
    unmatched = 0
    for call in report.calls:
        parts = urlsplit(call.url)
        found = next(
            (m for m, rx in patterns if m.http_method == call.method and rx.match(parts.path)),
            None,
        )
        if found is None:
            unmatched += 1
            continue
        call.endpoint = f"{found.http_method} {found.path}"
        args = ", ".join(f"{p.name}=..." for p in found.required)
        call.replacement = f"api.{found.name}({args})"
    report.next_step = f"ghost-api generate {site}  then use {spec.class_name}" + (
        f"; {unmatched} call(s) not mapped yet: ghost-api refresh {site}" if unmatched else ""
    )


def to_json(reports: list[HostReport]) -> list[dict[str, Any]]:
    return [asdict(r) for r in reports]
