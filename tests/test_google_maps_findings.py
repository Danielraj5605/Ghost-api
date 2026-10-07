"""Regressions from a real run on https://www.google.com/maps (2026-10-07).

That run stopped exploring at once ("'utf-8' codec can't decode byte 0x8b"), kept
Google's ")]}'"-guarded JSON without being able to read it, named a map-tile endpoint
with a 300-character method, and waited 20 s to "retry" a response that was not JSON.
"""

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from ghost_api.analysis.analyzer import analyze
from ghost_api.analysis.url_grouping import group_urls
from ghost_api.capture import filters
from ghost_api.capture.models import CapturedRequest
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.generator.naming import apply_plan, default_method_name
from ghost_api.generator.python_gen import write
from ghost_api.generator.spec import build_spec
from ghost_api.verify.classify import classify, short_reason

GZIP_START = b"\x1f\x8b\x08\x00\x00\x00\x00\x00"
GUARDED = ')]}\'\n[["2015-02-08", null, [1, 2]]]'


# --- capture: a fake Playwright response, enough for the recorder ---


class FakeRequest:
    def __init__(self, url: str, method: str = "GET", post: bytes | None = None) -> None:
        self.url, self.method, self.resource_type = url, method, "fetch"
        self.post_data_buffer = post

    def all_headers(self) -> dict[str, str]:
        return {"accept": "*/*"}


class FakeResponse:
    def __init__(
        self, url: str, body: bytes | Exception, ctype: str = "application/json", **kw: Any
    ) -> None:
        self.request = FakeRequest(url, **kw)
        self.status = 200
        self._body, self._ctype = body, ctype

    def all_headers(self) -> dict[str, str]:
        return {"content-type": self._ctype}

    def body(self) -> bytes:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def record(*responses: FakeResponse) -> TrafficRecorder:
    rec = TrafficRecorder("www.google.com")
    for r in responses:
        rec._on_response(r)  # type: ignore[arg-type]
    rec.flush()
    return rec


def test_binary_and_broken_responses_are_skipped_not_fatal() -> None:
    rec = record(
        FakeResponse("https://www.google.com/maps/rpc", GZIP_START),  # the original crash
        FakeResponse(
            "https://www.google.com/maps/rpc2", b"{}", method="POST", post=GZIP_START
        ),  # binary request body
        FakeResponse("https://www.google.com/maps/odd", RuntimeError("driver hiccup")),
        FakeResponse("https://www.google.com/maps/api/ok", b'{"ok": true}'),
    )
    assert [c.url for c in rec.captured] == ["https://www.google.com/maps/api/ok"]
    assert rec.dropped["binary"] == 2 and rec.dropped["unreadable"] == 1


def test_guarded_json_is_kept_and_stored_without_the_guard() -> None:
    rec = record(
        FakeResponse("https://www.google.com/maps/preview/pegman?pb=x", GUARDED.encode()),
        # "Says JSON but isn't": Google's length-prefixed batchexecute envelope.
        FakeResponse(
            "https://www.google.com/maps/_/data/batchexecute",
            b')]}\'\n\n130\n[["wrb.fr"]]\n25\n[["e",4]]',
        ),
        FakeResponse("https://www.google.com/maps/api/empty", b""),  # 204-style: kept
    )
    bodies = {c.url.split("?")[0]: c.resp_body for c in rec.captured}
    assert bodies["https://www.google.com/maps/preview/pegman"].startswith("[[")
    assert "https://www.google.com/maps/_/data/batchexecute" not in bodies
    assert "https://www.google.com/maps/api/empty" in bodies
    assert rec.dropped["invalid-json"] == 1


@pytest.mark.parametrize(
    "text",
    [GUARDED, "while(1);" + GUARDED[5:], "for(;;);" + GUARDED[5:], ")]}',\n" + GUARDED[5:]],
)
def test_parse_response_json_allows_guards(text: str) -> None:
    assert filters.parse_response_json(text)[0][0] == "2015-02-08"
    assert filters.strip_xssi('{"a": 1}') == '{"a": 1}'  # normal JSON untouched


# --- analysis and naming ---


def test_opaque_path_segments_become_parameters() -> None:
    tile = "https://www.google.com/maps/vt/pb=!1m8!3m7!1m2!1u94208!2u60416!2m2!1u1024"
    matches = group_urls(
        [
            ("GET", tile),
            ("GET", "https://x.test/maps/@12.9,77.6,15z"),
            ("GET", "https://x.test/api/search"),
        ]
    )
    assert [(m.template, m.param_names) for m in matches] == [
        ("/maps/vt/{pb}", ("pb",)),
        ("/maps/{id}", ("id",)),
        ("/api/search", ()),
    ]


def test_generated_names_stay_short() -> None:
    long_path = (
        "https://a.test/$rpc/google.internal.onegoogle.asyncdata.v1.AsyncDataService/GetAsyncData"
    )
    deep = "https://a.test/one/two/three/four/five"
    names = [default_method_name(ep) for ep in analyze([cap(long_path, "POST"), cap(deep)])]
    assert all(len(n) <= 70 for n in names), names
    assert "get_three_four_five" in names


def cap(url: str, method: str = "GET", body: str = '{"x": 1}') -> CapturedRequest:
    return CapturedRequest(
        method, url, 200, "fetch", {}, "{}" if method == "POST" else None,
        "application/json" if method == "POST" else None, "application/json", body,
    )  # fmt: skip


# --- verification ---


def test_not_json_is_its_own_failure_and_not_a_network_retry() -> None:
    # The pytest output includes the client's source, which mentions httpx.Timeout.
    message = (
        "timeout: httpx.Timeout = httpx.Timeout(30.0)\n"
        "E   google_client.GoogleError: HTTP 200: response is not JSON"
    )
    assert classify(message) == "not-json"
    assert short_reason("not-json", message) == "the response was not JSON (HTTP 200)"
    assert classify("timeout: httpx.Timeout(30.0)\nE   ShopError: HTTP 422: bad") == "unknown"
    assert classify("E   httpx.ReadTimeout: timed out") == "network"
    assert classify("E   ShopError: HTTP 0: network error: ConnectError") == "network"


# --- the generated client reads guarded JSON too ---


def test_generated_client_strips_the_guard(tmp_path: Path) -> None:
    # Captured as the recorder stores it: guard removed.
    requests = [cap("https://a.test/maps/preview/pegman", body=filters.strip_xssi(GUARDED))]
    eps = analyze(requests)
    eps[0].replayable = True
    cls, names = apply_plan(None, eps, "a.test")
    result = write(build_spec("a.test", "https://a.test/", eps, requests, cls, names), tmp_path)
    sys.path.insert(0, str(tmp_path))
    sys.modules.pop("models", None)
    try:
        module: ModuleType = importlib.import_module(result.module)
        api = getattr(module, cls)(
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, text=GUARDED))
            ),
            max_requests_per_second=0,
        )
        method = next(m for m in dir(api) if m.startswith("get_"))
        assert getattr(api, method)()[0][0] == "2015-02-08"
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop(result.module, None)
        sys.modules.pop("models", None)


# --- found by re-checking the fix on the real site: the client did not compile ---

# Shaped like Google's /maps/preview responses: arrays nested deep, mixing types and nulls.
MIXED = '[3, {"a": 1}, [7, "x", [[[1, [[2.5, null]]], null], null]], null]'


def test_nested_unions_keep_their_brackets() -> None:
    from ghost_api.generator.models_gen import _union

    # The old split gave "list[int", "None]", "list[str", "None]" and dropped the second
    # "None]" as a duplicate: "list[int | None] | list[str" (missing "]").
    merged = _union(["list[int | None]", "list[str | None]", "None"])
    assert merged == "list[int | None] | list[str | None] | None"
    assert merged.count("[") == merged.count("]")


def test_client_for_mixed_nested_arrays_compiles(tmp_path: Path) -> None:
    from ghost_api.generator.python_gen import check, render

    requests = [cap("https://a.test/maps/preview/passiveassist", body=MIXED)]
    eps = analyze(requests)
    eps[0].replayable = True
    cls, names = apply_plan(None, eps, "a.test")
    spec = build_spec("a.test", "https://a.test/", eps, requests, cls, names)
    rtype = spec.methods[0].response_type
    assert rtype.count("[") == rtype.count("]"), rtype
    check(render(spec))  # raises GenerationError if any file fails to compile


def test_table_labels_for_deep_arrays_stay_short() -> None:
    from ghost_api.analysis.schema import build_schema, top_level_fields

    deep = build_schema([[[[[[[[["x", 1]]]]]]]]])
    label = top_level_fields(deep)
    assert len(label) <= 60 and "…" in label, label
