"""Evaluate Ghost API on the sites in evals/sites.json and score the results.

    python -m evals.run_eval                         # all sites (uses LLM calls)
    python -m evals.run_eval --sites demo-shop       # one site
    python -m evals.run_eval --no-agent              # no LLM: checks the harness only

Each site runs the real `ghost-api run` in a subprocess with its own database, while
the peak memory of the whole process tree (Python + Chromium) is sampled. Then:

    recall     share of the goal's real endpoints that were found
    precision  share of reported endpoints that are real (the rest were invented)
    pass rate  share of generated methods whose live tests passed (verified)
    forbidden  data-changing endpoints that must never appear in generated output

Expected endpoints are written as "GET /api/products/{}" (any parameter name), with a
host for third-party APIs ("POST host/path"), or "*" for any host ("POST */path").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from ghost_api.analysis.models import Endpoint
from ghost_api.memory.store import Store

SITES_FILE = Path(__file__).parent / "sites.json"
RESULTS_DIR = Path(__file__).parent / "results"


@dataclass
class Site:
    name: str
    url: str
    goal: str
    expected: list[str]
    real: list[str] = field(default_factory=list)  # other real endpoints (not in the goal)
    forbidden: list[str] = field(default_factory=list)  # must never be generated

    @property
    def local(self) -> bool:
        return self.url.startswith("local:")


@dataclass
class Result:
    site: str
    status: str
    found: list[str]
    recall: float | None  # None when the site has no expected endpoints
    precision: float
    invented: list[str]
    verified: int
    unverified: int
    failed: int
    pass_rate: float | None
    llm_calls: int
    seconds: float
    peak_mb: float
    forbidden_generated: list[str]
    log: str = ""


# --- matching and scoring (pure, unit-tested) ---


def normalize(label: str) -> str:
    """'GET /api/products/{id}' -> 'GET /api/products/{}'."""
    return re.sub(r"\{[^}]*\}", "{}", label.strip())


def endpoint_label(ep: Endpoint, target_host: str) -> str:
    host = "" if ep.host == target_host else ep.host
    return normalize(f"{ep.method} {host}{ep.path_template}")


def matches(found: str, wanted: str) -> bool:
    found, wanted = normalize(found), normalize(wanted)
    if found == wanted:
        return True
    if " */" in wanted:  # any host
        method, path = wanted.split(" */", 1)
        f_method, f_rest = found.split(" ", 1)
        return f_method == method and f_rest.endswith("/" + path) and not f_rest.startswith("/")
    return False


def score(
    site: Site,
    endpoints: list[Endpoint],
    target_host: str,
    generated: list[str],
) -> dict[str, Any]:
    found = sorted({endpoint_label(e, target_host) for e in endpoints})
    real = site.expected + site.real
    hits = [w for w in site.expected if any(matches(f, w) for f in found)]
    genuine = [f for f in found if any(matches(f, w) for w in real)]
    tested = [e for e in endpoints if e.status in ("verified", "unverified", "failed")]
    verified = sum(e.status == "verified" for e in tested)
    return {
        "found": found,
        "recall": len(hits) / len(site.expected) if site.expected else None,
        "precision": len(genuine) / len(found) if found else 1.0,
        "invented": [f for f in found if f not in genuine],
        "verified": verified,
        "unverified": sum(e.status == "unverified" for e in tested),
        "failed": sum(e.status == "failed" for e in tested),
        "pass_rate": verified / len(tested) if tested else None,
        "forbidden_generated": [f for f in site.forbidden if any(matches(g, f) for g in generated)],
    }


def generated_operations(openapi_path: Path) -> list[str]:
    """'METHOD /path' for every operation in the exported OpenAPI file."""
    if not openapi_path.exists():
        return []
    doc = yaml.safe_load(openapi_path.read_text(encoding="utf-8")) or {}
    ops = []
    for path, item in (doc.get("paths") or {}).items():
        for method, op in item.items():
            servers = op.get("servers") or []
            host = urlsplit(servers[0]["url"]).hostname if servers else ""
            ops.append(normalize(f"{method.upper()} {host or ''}{path}"))
    return ops


# --- running ---


class PeakMemory:
    """Samples the resident memory of a process and all its children."""

    def __init__(self, pid: int, interval: float = 0.25) -> None:
        self.pid = pid
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> PeakMemory:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            import psutil
        except ImportError:  # optional: peak memory is reported as 0
            return
        try:
            root = psutil.Process(self.pid)
        except psutil.Error:
            return
        while not self._stop.is_set():
            try:
                procs = [root, *root.children(recursive=True)]
                total = sum(p.memory_info().rss for p in procs if p.is_running())
                self.peak = max(self.peak, total)
            except psutil.Error:
                pass
            self._stop.wait(self.interval)


def run_site(site: Site, args: argparse.Namespace, work: Path) -> Result:
    server = None
    url = site.url
    if site.local:
        from evals.demo_shop import serve

        server = serve(0)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
    db = work / f"{site.name}.db"
    out = work / "out"
    cmd = [
        sys.executable,
        "-m",
        "ghost_api",
        "run",
        url,
        "--goal",
        site.goal,
        "--db",
        str(db),
        "--out",
        str(out),
        "--max-llm-calls",
        str(args.max_llm_calls),
        "--max-steps",
        str(args.max_steps),
    ]
    if args.no_agent:
        cmd.append("--no-agent")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "160"}
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with PeakMemory(proc.pid) as memory:
            log, _ = proc.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        log = "(timed out)"
    finally:
        if server is not None:
            server.shutdown()
    seconds = time.monotonic() - started

    host = urlsplit(url).hostname or ""
    with Store(db) as store:
        run = store.latest_run(host)
        run_id = store.latest_endpoint_run(host)
        endpoints = store.load_endpoints(host, run_id) if run_id else []
    generated = generated_operations(out / host / "openapi.yaml")
    scores = score(site, endpoints, host, generated)
    return Result(
        site=site.name,
        status=run.status if run else "no run",
        llm_calls=run.llm_calls if run else 0,
        seconds=round(seconds, 1),
        peak_mb=round(memory.peak / 1e6, 1),
        log=log[-4000:],
        **scores,
    )


def load_sites(names: list[str] | None) -> list[Site]:
    sites = [Site(**s) for s in json.loads(SITES_FILE.read_text(encoding="utf-8"))]
    if names:
        unknown = set(names) - {s.name for s in sites}
        if unknown:
            raise SystemExit(f"Unknown site(s): {', '.join(sorted(unknown))}")
        sites = [s for s in sites if s.name in names]
    return sites


def to_markdown(results: list[Result], args: argparse.Namespace) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.0%}"

    lines = [
        "# Ghost API evaluation",
        "",
        f"Max {args.max_steps} agent steps and {args.max_llm_calls} LLM calls per site"
        + (", agent disabled (--no-agent)" if args.no_agent else "")
        + ".",
        "",
        "| Site | Outcome | Recall | Precision | Pass rate | LLM calls | Time | Peak memory |"
        " Invented | Forbidden generated |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.site} | {r.status} | {pct(r.recall)} | {pct(r.precision)} | "
            f"{pct(r.pass_rate)} | {r.llm_calls} | {r.seconds:.0f}s | {r.peak_mb:.0f} MB | "
            f"{', '.join(r.invented) or '-'} | {', '.join(r.forbidden_generated) or '-'} |"
        )
    lines += ["", "Found endpoints:", ""]
    for r in results:
        lines.append(f"- **{r.site}:** {', '.join(f'`{f}`' for f in r.found) or 'none'}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sites", help="Comma-separated site names (default: all).")
    parser.add_argument("--max-llm-calls", type=int, default=30)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=900, help="Seconds per site.")
    parser.add_argument("--no-agent", action="store_true", help="No LLM: harness check only.")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    sites = load_sites(args.sites.split(",") if args.sites else None)
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for site in sites:
            print(f"== {site.name}: {site.url}", flush=True)
            result = run_site(site, args, Path(tmp))
            results.append(result)
            print(
                f"   {result.status}; recall {result.recall}, precision {result.precision:.2f}, "
                f"{result.llm_calls} LLM calls, {result.seconds:.0f}s",
                flush=True,
            )
    args.results_dir.mkdir(parents=True, exist_ok=True)
    (args.results_dir / "results.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    markdown = to_markdown(results, args)
    (args.results_dir / "results.md").write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
