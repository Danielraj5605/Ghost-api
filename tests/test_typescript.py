"""TypeScript client generation: type mapping, strict type-checking, and running the
generated client in Node against a local server.

Tests that need Node.js or the TypeScript compiler are skipped when those are missing.
"""

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from ghost_api.analysis.analyzer import analyze
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.filters import REDACTED
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.generator.naming import apply_plan
from ghost_api.generator.spec import ClientSpec, build_spec
from ghost_api.generator.ts_gen import (
    camel,
    js_literal,
    render_typescript,
    ts_type,
    write_typescript,
)
from ghost_api.verify.runner import run_node_tests
from ghost_api.verify.verifier import Verifier

NODE = shutil.which("node")
NPX = shutil.which("npx")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

# --- conversions ---


@pytest.mark.parametrize(
    ("py", "ts"),
    [
        ("str", "string"),
        ("int | None", "number | null"),
        ("float | int", "number"),
        ("list[Quote]", "Quote[]"),
        ("list[int | str] | None", "(number | string)[] | null"),
        ("list[list[int | str]]", "(number | string)[][]"),
        ("dict[str, Any]", "Record<string, unknown>"),
        ("Any", "unknown"),
    ],
)
def test_ts_type(py: str, ts: str) -> None:
    assert ts_type(py) == ts


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("hits_per_page", "hitsPerPage"),
        ("class_", "classValue"),
        ("id", "id"),
        ("default", "defaultValue"),
        ("get_quotes", "getQuotes"),
    ],
)
def test_camel(name: str, expected: str) -> None:
    assert camel(name) == expected


def test_js_literal() -> None:
    assert js_literal("True") == "true"
    assert js_literal("['web', 1]") == '["web", 1]'
    assert js_literal("'en'") == '"en"'
    assert js_literal(None) == "undefined"


# --- a site with page pagination, a detail endpoint, a POST search and an API key ---


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _json(self, body: Any, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/items":
            if q.get("api_key") != "fake-removed":
                self._json({"error": "key"}, 401)
                return
            page = int(q.get("page", "1"))
            items = (
                [{"id": page * 10 + i, "itemName": f"n{i}"} for i in range(2)] if page <= 3 else []
            )
            self._json({"page": page, "has_next": page < 3, "items": items})
        elif url.path.startswith("/api/items/"):
            if self.headers.get("x-client") != "web":
                self._json({"error": "no"}, 403)
                return
            self._json({"id": int(url.path.rsplit("/", 1)[1]), "itemName": "x", "class": "a"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self._json({"hits": [body]})


@pytest.fixture
def shop() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def captured(
    url: str,
    body: Any,
    method: str = "GET",
    req_body: Any = None,
    headers: dict[str, str] | None = None,
) -> CapturedRequest:
    return CapturedRequest(
        method,
        url,
        200,
        "fetch",
        headers or {},
        json.dumps(req_body) if req_body is not None else None,
        "application/json" if req_body is not None else None,
        "application/json",
        json.dumps(body),
    )


def site(base: str) -> tuple[list[Endpoint], list[CapturedRequest], list[RawRequest | None]]:
    reqs = [
        captured(
            f"{base}/api/items?page={p}&api_key={REDACTED}",
            {"page": p, "has_next": True, "items": [{"id": p, "itemName": "n"}]},
        )
        for p in (1, 2)
    ] + [
        captured(
            f"{base}/api/items/11",
            {"id": 11, "itemName": "x", "class": "a"},
            headers={"x-client": "web"},
        ),
        captured(f"{base}/api/search", {"hits": []}, "POST", {"query": "dune", "page": 0}),
        captured(f"{base}/api/search", {"hits": []}, "POST", {"query": "foundation", "page": 0}),
    ]
    raws: list[RawRequest | None] = [
        RawRequest("GET", f"{base}/api/items?page={p}&api_key=fake-removed", {}) for p in (1, 2)
    ] + [
        RawRequest("GET", f"{base}/api/items/11", {"x-client": "web"}),
        RawRequest("POST", f"{base}/api/search", {}, json.dumps({"query": "dune", "page": 0})),
        RawRequest("POST", f"{base}/api/search", {}, json.dumps({"query": "x", "page": 0})),
    ]
    eps = analyze(reqs)
    for ep in eps:
        ep.replayable = True
        if ep.path_template == "/api/items":
            ep.pagination = {
                "style": "page",
                "param": "page",
                "location": "query",
                "size_param": None,
                "confirmed": True,
            }
    return eps, reqs, raws


def spec_for(base: str, header_known: bool = True) -> ClientSpec:
    eps, reqs, _ = site(base)
    if header_known:
        next(e for e in eps if e.path_template == "/api/items/{id}").required_headers = ["x-client"]
    cls, names = apply_plan(None, eps, "127.0.0.1")
    return build_spec("127.0.0.1", f"{base}/", eps, reqs, cls, names)


def test_rendered_typescript_has_no_secrets_and_camel_case_api() -> None:
    files = render_typescript(spec_for("http://127.0.0.1:1"))
    client = (
        files["n127_0_0_1_client.ts"]
        if "n127_0_0_1_client.ts" in files
        else next(src for name, src in files.items() if name.endswith("_client.ts"))
    )
    assert "async getItems(" in client and "async *iterGetItems(" in client
    assert "async postSearch(" in client and '"x-client": "web"' in client
    assert REDACTED not in client and '"fake-removed"' not in client  # the value, not the env name


@pytest.mark.skipif(NPX is None, reason="npx is not installed")
def test_generated_typescript_passes_tsc_strict(tmp_path: Path) -> None:
    result = write_typescript(spec_for("http://127.0.0.1:1"), tmp_path)
    proc = subprocess.run(
        [
            NPX or "npx",
            "--yes",
            "--package",
            "typescript@5",
            "tsc",
            "--noEmit",
            "--strict",
            "--target",
            "es2022",
            "--lib",
            "es2022,dom",
            "--module",
            "esnext",
            "--moduleResolution",
            "bundler",
            result.client_path.name,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        shell=False,
    )
    if proc.returncode != 0 and "npm" in proc.stderr and "ERR" in proc.stderr:
        pytest.skip(f"could not fetch the TypeScript compiler: {proc.stderr[-200:]}")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@needs_node
def test_generated_typescript_runs_against_a_live_server(shop: str, tmp_path: Path) -> None:
    result = write_typescript(spec_for(shop), tmp_path)
    module = result.client_path.name
    cls = result.client_path.read_text(encoding="utf-8").split("export class ")[2].split(" ")[0]
    cred_env = spec_for(shop).credentials[0].env
    script = tmp_path / "check.ts"
    script.write_text(
        f"""import {{ {cls} }} from "./{module}";
const api = new {cls}({{ maxRequestsPerSecond: 0 }});
const page = await api.getItems();
const pages = [];
for await (const p of api.iterGetItems()) pages.push(p.page);
const item = await api.getItem(11);
const search = await api.postSearch("dune");
let error = "";
try {{ await new {cls}({{ apiKey: "fake-removed" }}).getItems(); }} catch (e) {{
  error = `${{(e as Error).name}} ${{(e as {{ status: number }}).status}}`;
}}
console.log(JSON.stringify({{ first: page.items[0].itemName, pages, item: item.class,
  hit: search.hits[0], error }}));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE or "node", script.name],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, cred_env: "fake-removed"},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["first"] == "n0"
    assert out["pages"] == [1, 2, 3]  # stops on has_next=false
    assert out["item"] == "a"  # the required x-client header was sent
    assert out["hit"] == {"query": "dune", "page": 0}  # body defaults applied
    assert out["error"].endswith(" 401")


@needs_node
def test_user_agent_is_sent_in_node_but_not_promised_in_browsers(tmp_path: Path) -> None:
    result = write_typescript(spec_for("http://127.0.0.1:1"), tmp_path)
    module = result.client_path.name
    source = result.client_path.read_text(encoding="utf-8")
    cls = source.split("export class ")[2].split(" ")[0]
    assert "browsers don't let code set the User-Agent" in source
    script = tmp_path / "ua.ts"
    script.write_text(
        f"""import {{ {cls} }} from "./{module}";
const seen: (string | undefined)[] = [];
const fake = (async (_url: URL, init: RequestInit) => {{
  seen.push((init.headers as Record<string, string>)["User-Agent"]);
  return new Response('{{"items": [], "page": 1, "has_next": false}}', {{ status: 200 }});
}}) as unknown as typeof fetch;
const api = new {cls}({{ fetch: fake, maxRequestsPerSecond: 0, apiKey: "k" }});
await api.getItems();
(globalThis as {{ document?: unknown }}).document = {{}};  // as in a web page
await api.getItems();
console.log(JSON.stringify(seen));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE or "node", script.name], cwd=tmp_path, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    node_ua, browser_ua = json.loads(proc.stdout.strip().splitlines()[-1].replace("null", '"-"'))
    assert "GhostAPI/" in node_ua and browser_ua == "-"


@needs_node
def test_generated_node_tests_pass_through_the_runner(shop: str, tmp_path: Path) -> None:
    result = write_typescript(spec_for(shop), tmp_path)
    outcomes = run_node_tests(result.tests_path, {spec_for(shop).credentials[0].env: "fake-removed"})
    assert outcomes, "no tests ran"
    failed = [o for o in outcomes if o.outcome == "failed"]
    assert not failed, failed[0].message


@needs_node
def test_verifier_repairs_a_typescript_client(shop: str, tmp_path: Path) -> None:
    """The header isn't known to be required at first: Node tests fail with 403, the
    verifier re-probes, finds x-client, regenerates, and the TypeScript tests pass."""
    from ghost_api.net import RateLimiter, RobotsPolicy
    from ghost_api.probe.replay import Replayer

    eps, reqs, raws = site(shop)
    replayer = Replayer("127.0.0.1", RateLimiter(10_000), RobotsPolicy.allow_all("127.0.0.1"))
    verifier = Verifier(
        "127.0.0.1", f"{shop}/", eps, reqs, tmp_path, raw=raws, replayer=replayer, lang="typescript"
    )
    report = verifier.run()
    replayer.close()
    assert report.result is not None and report.result.client.client_path.suffix == ".ts"
    first = {o.name: o.outcome for o in report.rounds[0].outcomes}
    assert first["test_get_item"] == "failed"
    assert any(r.method == "get_item" and r.kind == "auth" for r in report.rounds[0].repairs)
    last = {o.name: o.outcome for o in report.rounds[-1].outcomes}
    assert all(v == "passed" for v in last.values()), last
    assert {s for s, _ in report.statuses.values()} == {"verified"}
