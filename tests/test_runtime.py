"""SiteRuntime: run a site's endpoints by name from stored data (fetch/watch/serve base)."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from evals.demo_shop import serve as serve_shop
from ghost_api.analysis.analyzer import analyze
from ghost_api.capture.models import CapturedRequest
from ghost_api.events import EventBus
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.pipeline import RunOptions, explore_site, finish, open_session
from ghost_api.probe.replay import Replayer
from ghost_api.runtime import OperationError, SiteRuntime, flatten, records_of


def fast_replayer(host: str) -> Replayer:
    return Replayer(host, RateLimiter(10_000), RobotsPolicy.allow_all(host), max_requests=1000)


@pytest.fixture(scope="module")
def shop_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, str]]:
    """A real no-LLM run on the demo shop, stored like any other run."""
    tmp = tmp_path_factory.mktemp("rt")
    httpd = serve_shop(0)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    db = tmp / "t.db"
    with Store(db) as store:
        s = open_session(
            store, EventBus(), "127.0.0.1", url, "list products", False, RateLimiter(50),
            RobotsPolicy.allow_all("127.0.0.1"), None, 30,
        )  # fmt: skip
        opts = RunOptions(out=tmp / "out", no_generate=True, no_verify=True)
        status, reason = explore_site(s, opts, use_agent=False)
        finish(s, status, reason, 0.0, opts)
    yield db, url
    httpd.shutdown()


def test_operations_have_names_inputs_and_pagination(shop_db: tuple[Path, str]) -> None:
    db, _ = shop_db
    with Store(db) as store, SiteRuntime(store, "127.0.0.1", fast_replayer("127.0.0.1")) as rt:
        ops = {op.name: op for op in rt.operations()}
        products = ops["get_products"]
        assert products.pagination and products.pagination["param"] == "page"
        assert {i.name for i in products.inputs} >= {"page", "per_page"}
        assert rt.operation("GET /api/products") is products or rt.operation("get_products")
        with pytest.raises(OperationError, match="Choose one of"):
            rt.operation("delete_everything")


def test_iterate_follows_pages_and_stops(shop_db: tuple[Path, str]) -> None:
    db, _ = shop_db
    with Store(db) as store, SiteRuntime(store, "127.0.0.1", fast_replayer("127.0.0.1")) as rt:
        op = rt.operation("get_products")
        pages = list(rt.iterate(op, {"per_page": "6"}, max_pages=20, max_rows=1000))
        rows = [r for p in pages for r in p.records]
        assert len(rows) == 30 and len({r["id"] for r in rows}) == 30  # all 5 pages
        assert [p.number for p in pages] == [1, 2, 3, 4, 5]
        capped = [r for p in rt.iterate(op, {}, max_pages=20, max_rows=10) for r in p.records]
        assert len(capped) == 10
        one_page = list(rt.iterate(op, {}, max_pages=1))
        assert len(one_page) == 1


def test_records_and_flatten() -> None:
    assert records_of({"total": 2, "items": [{"a": 1}, {"a": 2}]}) == [{"a": 1}, {"a": 2}]
    assert records_of({"id": 7, "name": "x"}) == [{"id": 7, "name": "x"}]  # one record
    assert records_of("text") == []
    row = flatten({"venue": {"name": "X", "loc": {"lat": 1}}, "tags": ["a", "b"],
                   "courts": [{"n": 1}]})  # fmt: skip
    assert row == {"venue.name": "X", "venue.loc.lat": 1, "tags": "a; b",
                   "courts": json.dumps([{"n": 1}])}  # fmt: skip


# --- a POST search the user allowed ---


class SearchHandler(BaseHTTPRequestHandler):
    bodies: list[Any] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SearchHandler.bodies.append(body)
        skip = int(body.get("skip", 0))
        venues = [{"id": i, "sport": body["sport"][0]} for i in range(skip, min(skip + 2, 5))]
        data = json.dumps({"data": {"final_venues": venues, "all_venues": 5}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def search_server() -> Iterator[str]:
    SearchHandler.bodies = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), SearchHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def stored_search(store: Store, base: str) -> None:
    body = {"area": "velachery", "sport": ["football"], "limit": 2, "skip": 0}
    req = CapturedRequest(
        "POST", f"{base}/api/venues_around_you", 200, "fetch",
        {"content-type": "application/json"}, json.dumps(body), "application/json",
        "application/json",
        json.dumps({"data": {"final_venues": [{"id": 0, "sport": "football"}], "all_venues": 5}}),
    )  # fmt: skip
    run_id = store.create_run("127.0.0.1", f"{base}/", "venues")
    store.add_request(run_id, req)
    store.save_endpoints("127.0.0.1", run_id, analyze([req]))


def test_a_post_search_runs_only_once_allowed(tmp_path: Path, search_server: str) -> None:
    with Store(tmp_path / "t.db") as store:
        stored_search(store, search_server)
        rt = SiteRuntime(store, "127.0.0.1", fast_replayer("127.0.0.1"))
        assert rt.operations() == []  # a POST without a read word: never called by itself
        store.allow_endpoints("127.0.0.1", [rt.endpoints[0].key])
        rt = SiteRuntime(store, "127.0.0.1", fast_replayer("127.0.0.1"))
    (op,) = rt.operations()
    assert op.allowed and op.pagination and op.pagination["param"] == "skip"
    assert {i.name: i.type for i in op.inputs}["sport"] == "list"
    # "cricket" for a list field becomes ["cricket"]; skip/limit paging is followed.
    rows = [r for p in rt.iterate(op, {"sport": "cricket"}, max_pages=10) for r in p.records]
    rt.close()
    assert [r["id"] for r in rows] == [0, 1, 2, 3, 4]
    assert SearchHandler.bodies[0]["sport"] == ["cricket"]
    assert [b["skip"] for b in SearchHandler.bodies] == [0, 2, 4, 5]  # offset += rows seen


def test_allow_is_remembered_by_later_generates(tmp_path: Path, search_server: str) -> None:
    from ghost_api.generator import NothingToGenerate, generate_client

    out = tmp_path / "out"
    with Store(tmp_path / "t.db") as store:
        stored_search(store, search_server)
        with pytest.raises(NothingToGenerate):  # skipped: may change data
            generate_client(store, "127.0.0.1", out)
        with pytest.raises(NothingToGenerate, match="names no stored endpoint"):
            generate_client(store, "127.0.0.1", out, allow={"POST /api/nope"})
        first = generate_client(store, "127.0.0.1", out, allow={"POST /api/venues_around_you"})
        again = generate_client(store, "127.0.0.1", out)  # no --allow this time
    assert [m.http_method for m in first.spec.methods] == ["POST"]
    assert [m.name for m in again.spec.methods] == [m.name for m in first.spec.methods]
