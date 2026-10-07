"""Watch & alerts (usefulness phase 4): baseline, then exactly what changed, then alerts."""

import copy
import json
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import evals.demo_shop as shop
from ghost_api import watch
from ghost_api.cli import app, schedule_command
from ghost_api.memory.store import Store
from ghost_api.runtime import SiteRuntime
from tests.test_runtime import fast_replayer, shop_db  # noqa: F401


class Inbox(BaseHTTPRequestHandler):
    received: list[tuple[str, dict[str, str], bytes]] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["content-length"]))
        Inbox.received.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()


@pytest.fixture
def inbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    Inbox.received = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Inbox)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv("GHOST_API_NTFY_URL", base)  # ntfy:TOPIC goes here, not to ntfy.sh
    yield base
    httpd.shutdown()


@pytest.fixture
def products() -> Iterator[list[dict[str, Any]]]:
    saved = copy.deepcopy(shop.PRODUCTS)
    yield shop.PRODUCTS
    shop.PRODUCTS[:] = saved


def check(store: Store, watch_id: int, **kw: Any) -> watch.CheckResult:
    w = store.get_watch(watch_id)
    assert w is not None
    with SiteRuntime(store, "127.0.0.1", fast_replayer("127.0.0.1")) as rt:
        return watch.check(store, w, runtime=rt, **kw)


def test_baseline_then_exactly_what_changed(
    shop_db: tuple[Path, str],  # noqa: F811
    products: list[dict[str, Any]],
    inbox: str,
) -> None:
    db, _ = shop_db
    with Store(db) as store:
        watch_id = store.add_watch({
            "site": "127.0.0.1", "operation": "get_products", "params": {"per_page": "10"},
            "every_minutes": 60, "notify": [f"webhook:{inbox}/hook", "ntfy:my-ghost-topic"],
            "max_pages": 10,
        })  # fmt: skip
        first = check(store, watch_id)
        assert first.baseline and first.rows == 30 and not first.changes
        assert "watching 30 items" in first.headline() and Inbox.received == []

        third = next(p for p in products if p["id"] == 3)
        old_price = third["price"]
        third["price"] = old_price + 5
        products.append({**products[0], "id": 31, "name": "Books item 31"})
        products[:] = [p for p in products if p["id"] != 30]

        second = check(store, watch_id)
        assert second.counts == {"new": 1, "changed": 1, "gone": 1}
        lines = [c.line() for c in second.changes]
        assert lines[0] == "+ Books item 31"
        assert lines[1].startswith("~ ") and f"price: {old_price} -> {old_price + 5}" in lines[1]
        assert lines[2].startswith("- ") and "item 30" in lines[2]
        assert second.notified == ["webhook: sent", "ntfy: sent"]

        hook = next(r for r in Inbox.received if r[0] == "/hook")
        payload = json.loads(hook[2])
        assert payload["counts"] == {"new": 1, "changed": 1, "gone": 1}
        assert "1 new, 1 changed, 1 gone" in payload["text"]
        topic = next(r for r in Inbox.received if r[0] == "/my-ghost-topic")
        assert "+ Books item 31" in topic[2].decode() and topic[1]["Title"]

        assert not check(store, watch_id).changes  # nothing new since
        stored = store.watch_changes(watch_id)
        assert {c["kind"] for c in stored} == {"new", "changed", "gone"}


def test_unread_pages_are_not_reported_as_gone(
    shop_db: tuple[Path, str],  # noqa: F811
    products: list[dict[str, Any]],
) -> None:
    db, _ = shop_db
    with Store(db) as store:
        watch_id = store.add_watch({"site": "127.0.0.1", "operation": "get_products",
                                    "params": {"per_page": "10"}, "max_pages": 1})  # fmt: skip
        check(store, watch_id)  # baseline: first 10 only
        products[:] = [p for p in products if p["id"] != 30]  # on a page we never read
        result = check(store, watch_id)
        assert not result.complete and result.counts["gone"] == 0


def test_volatile_fields_and_tracking() -> None:
    old = {"k": {"data": {"name": "A", "price": 1, "updated_at": "x", "distance": "1.2"},
                 "gone": False}}  # fmt: skip
    new = {"k": {"name": "A", "price": 1, "updated_at": "y", "distance": "3.4"}}
    assert watch.diff(old, new, set(), complete=True) == []  # only volatile fields moved
    new["k"]["price"] = 2
    (change,) = watch.diff(old, new, set(), complete=True)
    assert change.fields == {"price": [1, 2]}
    assert watch.diff(old, new, {"name"}, complete=True) == []  # only name is tracked


def test_schedule_parsing_and_due() -> None:
    assert watch.parse_every("30m") == 30 and watch.parse_every("6h") == 360
    assert watch.parse_every("1d") == 1440 and watch.every_label(360) == "6h"
    with pytest.raises(ValueError, match="at most every 5 minutes"):
        watch.parse_every("1m")
    with pytest.raises(ValueError, match="Not a duration"):
        watch.parse_every("soon")
    recent = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    assert watch.is_due({"last_check_at": None, "every_minutes": 60})
    assert not watch.is_due({"last_check_at": recent, "every_minutes": 60})
    assert watch.is_due({"last_check_at": recent, "every_minutes": 5})


def test_site_errors_are_recorded_not_raised(tmp_path: Path) -> None:
    with Store(tmp_path / "t.db") as store:
        watch_id = store.add_watch({"site": "nowhere.test", "operation": "x"})
        w = store.get_watch(watch_id)
        assert w is not None
        result = watch.check(store, w)
        assert "Nothing stored for nowhere.test" in result.error
        assert (store.get_watch(watch_id) or {})["last_status"].startswith("error:")


def test_watch_commands(shop_db: tuple[Path, str]) -> None:  # noqa: F811
    db, _ = shop_db
    runner = CliRunner(env={"COLUMNS": "200"})  # tables wide enough not to wrap
    added = runner.invoke(app, ["watch", "add", "127.0.0.1", "get_products", "per_page=20",
                                "--every", "2h", "--db", str(db)])  # fmt: skip
    assert added.exit_code == 0, added.output
    assert "every 2h" in added.output and "watching 30 items" in added.output
    watch_id = int(added.output.split("Watching #")[1].split(":")[0])
    listed = runner.invoke(app, ["watch", "list", "--db", str(db)])
    assert "get_products per_page=20" in listed.output and "2h" in listed.output
    ran = runner.invoke(app, ["watch", "run", str(watch_id), "--db", str(db)])
    assert "no changes in 30 items" in ran.output
    due = runner.invoke(app, ["watch", "run", "--due", "--db", str(db)])
    assert f"#{watch_id}" not in due.output  # just checked: not due for 2 hours
    bad = runner.invoke(app, ["watch", "add", "127.0.0.1", "nope", "--db", str(db)])
    assert bad.exit_code == 1 and "Choose one of" in bad.output
    gone = runner.invoke(app, ["watch", "remove", str(watch_id), "--db", str(db)])
    assert (
        gone.exit_code == 0
        and runner.invoke(app, ["watch", "remove", str(watch_id), "--db", str(db)]).exit_code == 1
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler")
def test_schedule_builds_a_task_command(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    command = schedule_command(db, 45)
    assert command[:3] == ["schtasks", "/Create", "/F"] and "45" in command
    assert command[-1].endswith(f'watch run --due --db "{db.resolve()}"')
    assert schedule_command(db, 5000)[command.index("/MO") + 1] == "1439"


def test_dashboard_watch_routes(shop_db: tuple[Path, str], tmp_path: Path) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from ghost_api.web.server import RunManager, create_app

    db, _ = shop_db
    out = tmp_path / "out"
    client = TestClient(create_app(db, out, "t", manager=RunManager(db, out, lambda c: None)))
    auth = {"x-ghost-token": "t"}
    bad = client.post("/api/watches", headers=auth, json={
        "site": "127.0.0.1", "endpoint": "get_products", "notify": ["email:me"]})  # fmt: skip
    assert bad.status_code == 400 and "Unknown notification target" in bad.json()["detail"]
    made = client.post("/api/watches", headers=auth, json={
        "site": "127.0.0.1", "endpoint": "get_products", "params": {"per_page": "20"},
        "every": "1d"})  # fmt: skip
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["watch"]["every"] == "1d" and body["check"]["baseline"] is True
    watch_id = body["watch"]["id"]
    listed = client.get("/api/watches", headers=auth).json()
    assert any(w["id"] == watch_id and not w["due"] for w in listed["watches"])
    again = client.post(f"/api/watches/{watch_id}/check", headers=auth).json()
    assert again["rows"] == 30 and again["changes"] == []
    assert client.delete(f"/api/watches/{watch_id}", headers=auth).status_code == 200
    assert client.post(f"/api/watches/{watch_id}/check", headers=auth).status_code == 404
