"""Evaluate Ghost API on the sites in evals/sites.json and score the results.

    python -m evals.run_eval                         # all sites (uses LLM calls)
    python -m evals.run_eval --sites demo-shop       # one site
    python -m evals.run_eval --no-agent              # no LLM: checks the harness only
    python -m evals.run_eval --suite local --no-agent --check
                                                     # the local corpus, as CI runs it:
                                                     # fails if a score drops (thresholds.json)

Each site runs the real `ghost-api run` in a subprocess with its own database, while
the peak memory of the whole process tree (Python + Chromium) is sampled. Then:

    recall     share of the goal's real endpoints that were found
    precision  share of reported endpoints that are real (the rest were invented)
    pass rate  share of generated methods whose live tests passed (verified)
    forbidden  data-changing endpoints that must never appear in generated output
    checks     site-specific rules: a disallowed path never called, personal data never
               stored, a reply too big to read dropped, the expected run status...

Expected endpoints are written as "GET /api/products/{}" (any parameter name), with a
host for third-party APIs ("POST host/path"), or "*" for any host ("POST */path").
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from ghost_api.analysis.models import Endpoint
from ghost_api.memory.store import Store

SITES_FILE = Path(__file__).parent / "sites.json"
THRESHOLDS_FILE = Path(__file__).parent / "thresholds.json"
RESULTS_DIR = Path(__file__).parent / "results"


@dataclass
class Site:
    name: str
    url: str
    goal: str
    expected: list[str]
    real: list[str] = field(default_factory=list)  # other real endpoints (not in the goal)
    forbidden: list[str] = field(default_factory=list)  # must never be generated
    checks: dict[str, Any] = field(default_factory=dict)  # see check_rules()
    agent_only: bool = False  # only meaningful with the AI agent (skipped with --no-agent)
    # What a run without the agent (page load and scroll only) can find, when it is less.
    expected_no_agent: list[str] | None = None

    @property
    def local(self) -> bool:
        return self.url.startswith("local:")

    @property
    def suite(self) -> str:
        return "local" if self.local else "live"


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
    checks_failed: list[str] = field(default_factory=list)
    log: str = ""


# --- matching and scoring (pure, unit-tested) ---


def normalize(label: str) -> str:
    """'GET /api/products/{id}' -> 'GET /api/products/{}'."""
    return re.sub(r"\{[^}]*\}", "{}", label.strip())


def endpoint_label(ep: Endpoint, target_host: str) -> str:
    host = "" if ep.host == target_host else ep.host
    return normalize(f"{ep.method} {host}{ep.path_template}")


def matches(found: str, wanted: str) -> bool:
    """Does a found endpoint match an expected one?

    `{}` in the expected path matches any one segment, so an endpoint seen with a single
    value (`/indexes/Item_dev/query`, not yet generalised to a template) still counts as
    found. `*/` as the host means "any other host" (a third-party API).
    """
    method, _, target = normalize(wanted).partition(" ")
    f_method, _, f_target = normalize(found).partition(" ")
    if method != f_method:
        return False
    if target.startswith("*/"):
        if f_target.startswith("/"):
            return False  # must be on another host
        f_target, target = f_target[f_target.index("/") :], target[1:]
    pattern = re.escape(target).replace(re.escape("{}"), r"[^/]+")
    return re.fullmatch(pattern, f_target) is not None


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


def check_rules(
    checks: dict[str, Any],
    status: str,
    endpoints: list[Endpoint],
    target_host: str,
    hits: list[str],
    stored_bodies: list[str],
    dropped: dict[str, int],
) -> list[str]:
    """The site's own rules; returns what failed, in plain words."""
    failed = []
    by_label = {endpoint_label(e, target_host): e for e in endpoints}

    def endpoint(label: str) -> Endpoint | None:
        return next((e for f, e in by_label.items() if matches(f, label)), None)

    if "status" in checks and status != checks["status"]:
        failed.append(f"status {status!r}, expected {checks['status']!r}")
    for prefix in checks.get("never_hit", []):
        called = [h for h in hits if h.split(" ", 1)[1].startswith(prefix)]
        if called:
            failed.append(f"{prefix} was requested {len(called)} time(s): {called[0]}")
    for value in checks.get("never_stored", []):
        if any(value in body for body in stored_bodies):
            failed.append(f"personal value {value[:4]}... was stored")
    for reason in checks.get("dropped", []):
        if not dropped.get(reason):
            failed.append(f"nothing was dropped as {reason!r}")
    for label, style in checks.get("pagination", {}).items():
        ep = endpoint(label)
        got = (ep.pagination or {}).get("style") if ep else None
        if got != style:
            failed.append(f"{label} paging {got!r}, expected {style!r}")
    for label, header in checks.get("required_header", {}).items():
        ep = endpoint(label)
        if ep is None or header not in [h.lower() for h in ep.required_headers]:
            failed.append(f"{label} should need the {header} header")
    return failed


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
    server: Any = None
    fixture: Any = None
    url = site.url
    if site.url == "local:demo_shop":
        from evals.demo_shop import serve

        server = serve(0)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
    elif site.url.startswith("local:fixture:"):
        from evals.fixtures import serve as serve_fixture

        fixture = server = serve_fixture(site.url.removeprefix("local:fixture:"))
        url = fixture.url
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
    dropped: dict[str, int] = {}
    bodies: list[str] = []
    with Store(db) if db.exists() else _no_store() as store:
        run = store.latest_run(host) if store else None
        run_id = store.latest_endpoint_run(host) if store else None
        endpoints = store.load_endpoints(host, run_id) if store and run_id else []
        if store and run:
            bodies = [r.resp_body or "" for r in store.requests_for_domain(host)]
            for event in store.events_for_run(run.id):
                if event.get("type") == "run_finished":
                    dropped = event.get("filtered") or {}
    generated = generated_operations(out / host / "openapi.yaml")
    if args.no_agent and site.expected_no_agent is not None:
        site = replace(site, expected=site.expected_no_agent)
    scores = score(site, endpoints, host, generated)
    status = run.status if run else "no run"
    hits = fixture.hits if fixture is not None else []
    checks = dict(site.checks)
    if "never_stored" in checks:  # "$PERSON_EMAIL": a value built at run time in fixtures.py
        from evals import fixtures

        checks["never_stored"] = [
            getattr(fixtures, v[1:]) if v.startswith("$") else v for v in checks["never_stored"]
        ]
    failed = check_rules(checks, status, endpoints, host, hits, bodies, dropped)
    return Result(
        site=site.name,
        status=status,
        checks_failed=failed,
        llm_calls=run.llm_calls if run else 0,
        seconds=round(seconds, 1),
        peak_mb=round(memory.peak / 1e6, 1),
        log=log[-4000:],
        **scores,
    )


@contextlib.contextmanager
def _no_store() -> Iterator[None]:
    yield None


def load_sites(names: list[str] | None) -> list[Site]:
    sites = [Site(**s) for s in json.loads(SITES_FILE.read_text(encoding="utf-8"))]
    if names:
        unknown = set(names) - {s.name for s in sites}
        if unknown:
            raise SystemExit(f"Unknown site(s): {', '.join(sorted(unknown))}")
        sites = [s for s in sites if s.name in names]
    return sites


def gate(results: list[Result], limits: dict[str, Any]) -> list[str]:
    """Every way the results fall below the thresholds (empty: the gate passes)."""
    problems = []
    for r in results:
        if r.recall is not None and r.recall < limits.get("min_recall", 0):
            problems.append(f"{r.site}: recall {r.recall:.0%}")
        if r.precision < limits.get("min_precision", 0):
            problems.append(f"{r.site}: precision {r.precision:.0%} (invented: {r.invented})")
        if len(r.forbidden_generated) > limits.get("max_forbidden_generated", 0):
            problems.append(f"{r.site}: generated forbidden {r.forbidden_generated}")
        if len(r.checks_failed) > limits.get("max_failed_checks", 0):
            problems.append(f"{r.site}: {'; '.join(r.checks_failed)}")
        if r.seconds > limits.get("max_seconds_per_site", float("inf")):
            problems.append(f"{r.site}: took {r.seconds:.0f}s")
        if r.peak_mb > limits.get("max_peak_mb", float("inf")):
            problems.append(f"{r.site}: peak memory {r.peak_mb:.0f} MB")
    return problems


def environment() -> dict[str, str]:
    from ghost_api import __version__

    return {
        "ghost_api": __version__,
        "date": time.strftime("%Y-%m-%d"),
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
    }


def to_markdown(results: list[Result], args: argparse.Namespace) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.0%}"

    env = environment()
    lines = [
        "# Ghost API evaluation",
        "",
        f"Ghost API {env['ghost_api']}, {env['date']}, Python {env['python']}, {env['os']}.",
        "",
        f"Max {args.max_steps} agent steps and {args.max_llm_calls} LLM calls per site"
        + (", agent disabled (--no-agent)" if args.no_agent else "")
        + ".",
        "",
        "| Site | Outcome | Recall | Precision | Pass rate | LLM calls | Time | Peak memory |"
        " Invented | Forbidden generated | Failed checks |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.site} | {r.status} | {pct(r.recall)} | {pct(r.precision)} | "
            f"{pct(r.pass_rate)} | {r.llm_calls} | {r.seconds:.0f}s | {r.peak_mb:.0f} MB | "
            f"{', '.join(r.invented) or '-'} | {', '.join(r.forbidden_generated) or '-'} | "
            f"{'; '.join(r.checks_failed) or '-'} |"
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
    parser.add_argument(
        "--suite", choices=("local", "live", "all"), default="all",
        help="local: the test sites on this machine (deterministic, for CI); live: real sites.",
    )  # fmt: skip
    parser.add_argument(
        "--check", action="store_true", help="Exit 1 if a score is below evals/thresholds.json."
    )
    args = parser.parse_args(argv)

    sites = load_sites(args.sites.split(",") if args.sites else None)
    if args.suite != "all":
        sites = [s for s in sites if s.suite == args.suite]
    if args.no_agent:
        sites = [s for s in sites if not s.agent_only]
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
        json.dumps(
            {"environment": environment(), "results": [asdict(r) for r in results]}, indent=2
        ),
        encoding="utf-8",
    )
    markdown = to_markdown(results, args)
    (args.results_dir / "results.md").write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    if args.check:
        limits = json.loads(THRESHOLDS_FILE.read_text(encoding="utf-8"))
        problems = gate(results, limits.get(args.suite if args.suite != "all" else "local", {}))
        if problems:
            print("Below the thresholds:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print("All thresholds met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
