"""Stage 7: classify failures, repair, and re-verify."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from ghost_api.analysis.analyzer import analyze
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.probe.replay import Replayer
from ghost_api.verify.classify import classify, validation_locations
from ghost_api.verify.repair import relax_schema
from ghost_api.verify.runner import TestOutcome, method_of, parse_junit
from ghost_api.verify.verifier import Verifier

# --- classification ---


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("ShopError: HTTP 401: Unauthorized", "auth"),
        ("ShopError: HTTP 403: Forbidden", "auth"),
        ("ShopError: HTTP 404: Not Found", "not-found"),
        ("ShopError: HTTP 429: Too Many Requests", "rate-limited"),
        ("ShopError: HTTP 503: Service Unavailable", "network"),
        ("ShopError: network error: ConnectError", "network"),
        (
            "ShopError: Missing credential: pass api_key=... or set SHOP_API_KEY",
            "missing-credential",
        ),
        (
            "pydantic_core._pydantic_core.ValidationError: 1 validation error for X\nnote\n  Input",
            "schema",
        ),
        ("AssertionError: assert None is not None", "unknown"),
    ],
)
def test_classify(message: str, kind: str) -> None:
    assert classify(message) == kind


def test_validation_locations_from_pytest_output() -> None:
    message = (
        "E   pydantic_core._pydantic_core.ValidationError: 2 validation errors for Page\n"
        "E   items.0.note\n"
        "E     Input should be a valid string [type=string_type, input_value=None]\n"
        "E       For further information visit https://errors.pydantic.dev\n"
        "E   page\n"
        "E     Input should be a valid integer\n"
    )
    assert validation_locations(message) == [("items", "0", "note"), ("page",)]


def test_relax_schema() -> None:
    schema = {
        "type": "object",
        "required": ["items", "page"],
        "properties": {
            "page": {"type": "integer"},
            "items": {"type": "array", "items": {"type": "object", "required": ["note", "id"],
                      "properties": {"note": {"type": "string"}, "id": {"type": "integer"}}}},
        },
    }  # fmt: skip
    out = relax_schema(schema, [("items", "0", "note"), ("page",), ("missing", "x")])
    assert out is not None
    item = out["properties"]["items"]["items"]
    assert item["properties"]["note"] == {} and item["required"] == ["id"]
    assert out["properties"]["page"] == {} and out["required"] == ["items"]
    assert schema["properties"]["page"] == {"type": "integer"}  # original untouched


def test_parse_junit() -> None:
    xml = """<testsuites><testsuite>
      <testcase name="test_get_items"/>
      <testcase name="test_iter_get_items"><failure message="HTTP 500">trace</failure></testcase>
      <testcase name="test_get_item"><skipped message="set SHOP_KEY to run this test"/></testcase>
    </testsuite></testsuites>"""
    outcomes = parse_junit(xml)
    assert [(o.method, o.outcome) for o in outcomes] == [
        ("get_items", "passed"), ("get_items", "failed"), ("get_item", "skipped"),
    ]  # fmt: skip
    assert "HTTP 500" in outcomes[1].message and method_of("test_iter_x") == "x"


# --- verifier logic with a scripted test runner ---

HOST = "shop.test"
START = f"https://{HOST}/"


def cap(url: str, body: Any) -> CapturedRequest:
    return CapturedRequest("GET", url, 200, "fetch", {}, None, None, "application/json",
                           json.dumps(body))  # fmt: skip


def scripted(*rounds: dict[str, tuple[str, str]]) -> Any:
    """A fake test runner: each call returns the next round's {method: (outcome, msg)}."""
    queue = list(rounds)
    seen_env: list[dict[str, str]] = []

    def runner(path: Path, env: dict[str, str]) -> list[TestOutcome]:
        seen_env.append(env)
        current = queue.pop(0) if len(queue) > 1 else queue[0]
        return [TestOutcome(f"test_{m}", m, o, msg) for m, (o, msg) in current.items()]  # type: ignore[arg-type]

    runner.seen_env = seen_env  # type: ignore[attr-defined]
    return runner


def make_verifier(tmp_path: Path, runner: Any, **kw: Any) -> Verifier:
    requests = [cap(f"https://{HOST}/api/items?page=1&api_key=K1", {"items": [{"id": 1}]})]
    raw = [RawRequest("GET", f"https://{HOST}/api/items?page=1&api_key=K1", {})]
    endpoints = analyze(requests)
    return Verifier(HOST, START, endpoints, requests, tmp_path, raw=raw, test_runner=runner,
                    sleep=lambda s: None, **kw)  # fmt: skip


SCHEMA_FAIL = ("failed", "ValidationError: 1 validation error for X\nitems.0.id\n  Input should")


def test_all_tests_pass_in_one_round(tmp_path: Path) -> None:
    v = make_verifier(tmp_path, scripted({"get_items": ("passed", "")}))
    report = v.run()
    assert len(report.rounds) == 1
    assert list(report.statuses.values()) == [("verified", "1 live test(s) passed")]


def test_schema_failure_is_relaxed_then_passes(tmp_path: Path) -> None:
    v = make_verifier(tmp_path, scripted({"get_items": SCHEMA_FAIL}, {"get_items": ("passed", "")}))
    report = v.run()
    assert [r.action for r in report.rounds[0].repairs] == [
        "relaxed fields that did not match: items.id"
    ]
    assert v.endpoints[0].response_schema["properties"]["items"]["items"]["properties"]["id"] == {}  # type: ignore[index]
    assert list(report.statuses.values())[0][0] == "verified"


def test_schema_failure_collects_samples_first_when_it_can(tmp_path: Path) -> None:
    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "a-string-now"}]})

    replayer = Replayer(HOST, RateLimiter(10_000), RobotsPolicy.allow_all(HOST),
                        client=httpx.Client(transport=httpx.MockTransport(api)))  # fmt: skip
    v = make_verifier(tmp_path, scripted({"get_items": SCHEMA_FAIL}, {"get_items": ("passed", "")}),
                      replayer=replayer)  # fmt: skip
    report = v.run()
    assert report.rounds[0].repairs[0].action == "collected fresh samples to widen the model"
    id_type = v.endpoints[0].response_schema["properties"]["items"]["items"]["properties"]["id"]  # type: ignore[index]
    assert set(id_type["type"]) == {"integer", "string"}
    assert len(v.requests) > 1


def test_persistent_auth_failure_ends_as_failed(tmp_path: Path) -> None:
    v = make_verifier(tmp_path, scripted({"get_items": ("failed", "HTTP 403: Forbidden")}))
    report = v.run()
    assert len(report.rounds) == 1  # no repair possible without a replayer or agent
    status, note = list(report.statuses.values())[0]
    assert status == "failed" and "HTTP 403" in note


def test_auth_failure_sends_the_agent_back(tmp_path: Path) -> None:
    goals: list[str] = []
    v = make_verifier(tmp_path, scripted({"get_items": ("failed", "HTTP 401")},
                                         {"get_items": ("passed", "")}),
                      explore=goals.append)  # fmt: skip
    report = v.run()
    assert goals and "fresh session" in goals[0]
    assert list(report.statuses.values())[0][0] == "verified"


def test_rate_limit_backs_off_once_and_stays_unverified(tmp_path: Path) -> None:
    sleeps: list[float] = []
    runner = scripted({"get_items": ("failed", "HTTP 429: Too Many Requests")})
    requests = [cap(f"https://{HOST}/api/items", {"items": []})]
    v = Verifier(HOST, START, analyze(requests), requests, tmp_path, test_runner=runner,
                 sleep=sleeps.append)  # fmt: skip
    report = v.run()
    assert sleeps == [20.0] and len(report.rounds) == 2
    assert list(report.statuses.values())[0][0] == "unverified"


def test_skipped_tests_are_unverified(tmp_path: Path) -> None:
    v = make_verifier(tmp_path, scripted({"get_items": ("skipped", "set SHOP_KEY to run")}))
    assert list(v.run().statuses.values()) == [("unverified", "set SHOP_KEY to run")]


def test_credentials_are_passed_to_tests_from_memory(tmp_path: Path) -> None:
    runner = scripted({"get_items": ("passed", "")})
    make_verifier(tmp_path, runner).run()
    assert runner.seen_env[0] == {"SHOP_TEST_API_KEY": "K1"}
    generated = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / HOST).glob("*.py"))
    assert "K1" not in generated  # never written to disk


# --- the Stage 7 "done when" test: real server, real generated tests, real repairs ---


class Handler(BaseHTTPRequestHandler):
    note_value: Any = "text"  # flipped to None after capture to break the model

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/api/notes"):
            body = {"notes": [{"id": 1, "note": Handler.note_value}]}
            status = 200
        elif self.path.startswith("/api/secure"):
            ok = self.headers.get("x-client") == "web"
            body, status = ({"items": [{"id": 1}]}, 200) if ok else ({"error": "no"}, 403)
        else:
            body, status = {}, 404
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server() -> Iterator[str]:
    Handler.note_value = "text"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_verifier_repairs_broken_schema_and_missing_header(server: str, tmp_path: Path) -> None:
    host = "127.0.0.1"
    # What exploring captured: "note" was always a string, and the browser happened to send
    # the x-client header (but probing never ran, so it isn't known to be required).
    requests = [
        CapturedRequest("GET", f"{server}/api/notes", 200, "fetch", {}, None, None,
                        "application/json", json.dumps({"notes": [{"id": 1, "note": "text"}]})),
        CapturedRequest("GET", f"{server}/api/secure", 200, "fetch", {"x-client": "web"}, None,
                        None, "application/json", json.dumps({"items": [{"id": 1}]})),
    ]  # fmt: skip
    raw: list[RawRequest | None] = [
        RawRequest("GET", f"{server}/api/notes", {"accept": "application/json"}),
        RawRequest(
            "GET", f"{server}/api/secure", {"accept": "application/json", "x-client": "web"}
        ),
    ]
    endpoints: list[Endpoint] = analyze(requests)
    Handler.note_value = None  # the live API now returns null: the model is wrong

    replayer = Replayer(host, RateLimiter(10_000), RobotsPolicy.allow_all(host))
    verifier = Verifier(host, f"{server}/", endpoints, requests, tmp_path, raw=raw,
                        replayer=replayer)  # fmt: skip
    report = verifier.run()
    replayer.close()

    first = {o.name: o.outcome for o in report.rounds[0].outcomes}
    assert first == {"test_get_notes": "failed", "test_get_secure": "failed"}
    actions = {r.method: (r.kind, r.action) for r in report.rounds[0].repairs}
    assert actions["get_notes"] == ("schema", "collected fresh samples to widen the model")
    assert actions["get_secure"] == ("auth", "re-tested which headers are required")

    last = {o.name: o.outcome for o in report.rounds[-1].outcomes}
    assert last == {"test_get_notes": "passed", "test_get_secure": "passed"}
    assert {s for s, _ in report.statuses.values()} == {"verified"}
    secure = next(e for e in verifier.endpoints if e.path_template == "/api/secure")
    assert secure.required_headers == ["x-client"]


def test_run_tests_with_a_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ghost_api.verify.runner import run_tests

    folder = tmp_path / "output" / "site.test"
    folder.mkdir(parents=True)
    source = "import os\n\n\ndef test_ok():\n    assert os.environ['GHOST_X'] == '1'\n"
    (folder / "test_site.py").write_text(source, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    outcomes = run_tests(Path("output/site.test/test_site.py"), {"GHOST_X": "1"})
    assert [(o.name, o.outcome) for o in outcomes] == [("test_ok", "passed")]
