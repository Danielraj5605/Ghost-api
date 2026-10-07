"""UI phase 5: the shareable one-file report.html."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from ghost_api.export import write_exports
from ghost_api.export.html import DATA_TAG, TEMPLATE, ViewerMissing, build_report_html, embed_json
from ghost_api.memory.store import Store
from tests.test_export import HOST, populate


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with Store(tmp_path / "t.db") as s:
        yield s


def embedded(page: str) -> dict[str, Any]:
    match = re.search(r'<script id="ghost-report"[^>]*>(.*?)</script>', page, re.S)
    assert match, "no data element"
    data: dict[str, Any] = json.loads(match.group(1))
    return data


def test_embedded_json_cannot_break_out_of_the_script_element() -> None:
    nasty = {"a": "</script><script>alert(1)</script>", "b": "<!-- x", "c": "&amp;", "d": " "}
    text = embed_json(nasty)
    assert "</script" not in text and "<!--" not in text and "&" not in text
    assert json.loads(text) == nasty


def test_template_is_built_and_self_contained() -> None:
    page = TEMPLATE.read_text(encoding="utf-8")
    assert page.count(DATA_TAG) == 1
    assert not re.search(r"<script[^>]*\bsrc=", page)  # scripts are inlined
    assert not re.search(r"<link[^>]*stylesheet", page)  # styles are inlined


def test_missing_or_broken_template(tmp_path: Path) -> None:
    with pytest.raises(ViewerMissing):
        build_report_html({}, tmp_path / "nope.html")
    broken = tmp_path / "broken.html"
    broken.write_text("<html></html>", encoding="utf-8")
    with pytest.raises(ViewerMissing):
        build_report_html({}, broken)


def test_write_exports_writes_a_shareable_report(store: Store, tmp_path: Path) -> None:
    run_id = populate(store)
    store.add_event(run_id, {"type": "notice", "text": "hello", "level": "info", "detail": ""})
    store.add_event(
        run_id,
        {"type": "request", "method": "GET", "url": "https://shop.test/x?k=1", "status": 200},
    )
    store.add_event(run_id, {"type": "endpoints", "endpoints": [{"secret": "examples"}]})
    out = tmp_path / "out"
    folder = out / HOST
    folder.mkdir(parents=True)
    (folder / "shop_client.py").write_text("class ShopClient: ...\n", encoding="utf-8")
    (folder / "huge_client.ts").write_text("x" * 400_000, encoding="utf-8")

    exports = write_exports(store, HOST, run_id, out)
    assert exports.html == folder / "report.html"
    data = embedded(exports.html.read_text(encoding="utf-8"))

    assert data["domain"] == HOST and data["run"]["id"] == run_id
    assert data["run"]["status"] == "goal-covered"
    assert data["report_md"].startswith("# Ghost API report")
    labels = {e["label"] for e in data["endpoints"]}
    assert "GET /api/items" in labels
    items = next(e for e in data["endpoints"] if e["label"] == "GET /api/items")
    assert items["response_schema"]["type"] == "object"
    # Sensitive parameter examples are dropped; others are kept.
    params = {p["name"]: p for p in items["params"]}
    assert params["api_key"]["sensitive"] and params["api_key"]["examples"] == []
    assert params["page"]["examples"]
    # Only the activity worth sharing: no request URLs, no raw endpoint lists.
    assert [e["type"] for e in data["events"]] == ["notice"]
    files = {f["name"]: f for f in data["files"]}
    assert files["shop_client.py"]["content"].startswith("class ShopClient")
    assert files["huge_client.ts"]["content"] is None  # too big: listed, not embedded
    assert "openapi.yaml" in files and "report.md" not in files
    assert data["lessons"]


def test_exports_still_work_without_the_viewer(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ghost_api.export as export

    def missing(data: dict[str, Any]) -> str:
        raise ViewerMissing("not built")

    monkeypatch.setattr(export, "build_report_html", missing)
    exports = write_exports(store, HOST, populate(store), tmp_path / "out")
    assert exports.html is None and exports.report.exists()
