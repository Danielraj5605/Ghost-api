"""Phase 3: API drift, client versions and `ghost-api diff`."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ghost_api import drift, exitcodes
from ghost_api.analysis.analyzer import analyze
from ghost_api.capture.models import CapturedRequest
from ghost_api.cli import app
from ghost_api.memory.store import Store

runner = CliRunner()


def snap(methods: dict[str, dict[str, object]], models: dict[str, dict[str, object]]) -> dict:
    return {"methods": methods, "models": models}


def method(call: str = "GET /api/q", returns: str = "Page", **params: tuple[str, bool]) -> dict:
    return {
        "call": call,
        "returns": returns,
        "summary": "s",
        "params": {n: {"type": t, "required": r} for n, (t, r) in params.items()},
    }


def test_changes_are_classified() -> None:
    old = snap(
        {"get_q": method(page=("int", False)), "get_x": method("GET /api/x")},
        {"Page": {"items": {"type": "list[Item]", "optional": False},
                  "total": {"type": "int", "optional": False}}},
    )  # fmt: skip
    new = snap(
        {"get_q": method(page=("int", True), q=("str", False)), "get_y": method("GET /api/y")},
        {"Page": {"items": {"type": "list[Item]", "optional": False},
                  "total": {"type": "str", "optional": False},
                  "next": {"type": "str | None", "optional": True}}},
    )  # fmt: skip
    lines = {c.line() for c in drift.compare(old, new)}
    assert lines == {
        "! get_x: method removed (GET /api/x)",
        "+ get_y: new method (GET /api/y)",
        "! get_q(page): is now required",
        "+ get_q(q): new optional parameter",
        "! Page.total: type str instead of int",
        "+ Page.next: new field (str | None)",
    }


def test_optional_reply_fields_compare_the_safe_way() -> None:
    old = snap({}, {"M": {"a": {"type": "int | None", "optional": True},
                          "b": {"type": "str", "optional": False}}})  # fmt: skip
    new = snap({}, {"M": {"a": {"type": "int", "optional": False},
                          "b": {"type": "str | None", "optional": True}}})  # fmt: skip
    lines = {c.line() for c in drift.compare(old, new)}
    assert lines == {"~ M.a: is now always present", "! M.b: may now be missing (str | None)"}


def test_versions_follow_semver() -> None:
    br = [drift.Change("breaking", "a", "x")]
    add = [drift.Change("additive", "a", "x")]
    info = [drift.Change("info", "a", "x")]
    assert drift.bump("1.4.2", br + add) == "2.0.0"
    assert drift.bump("1.4.2", add + info) == "1.5.0"
    assert drift.bump("1.4.2", info) == "1.4.3"


def test_live_reply_changes() -> None:
    stored = {
        "type": "object", "required": ["items", "total"],
        "properties": {
            "items": {"type": "array", "items": {
                "type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}},
            "total": {"type": "integer"},
            "note": {"type": "string"},
        },
    }  # fmt: skip
    fresh = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {
                "type": "object", "properties": {"id": {"type": "string"}}}},
            "cursor": {"type": "string"},
        },
    }  # fmt: skip
    lines = {c.line() for c in drift.live_changes("get_q", stored, fresh)}
    assert "! get_q total: field missing from the reply" in lines
    assert "! get_q items[].id: now string, was integer" in lines
    assert "+ get_q cursor: new field in the reply" in lines
    assert not any("note" in line for line in lines)  # optional before: missing is fine


# --- with a store ---------------------------------------------------------------------------


def cap(url: str, body: object) -> CapturedRequest:
    return CapturedRequest("GET", url, 200, "fetch", {}, None, None, "application/json",
                           json.dumps(body))  # fmt: skip


def save_run(db: Path, reqs: list[CapturedRequest]) -> None:
    with Store(db) as store:
        run = store.create_run("ex.com", "https://ex.com/", "items")
        for r in reqs:
            store.add_request(run, r)
        store.save_endpoints("ex.com", run, analyze(reqs))


def items(n: int, **extra: object) -> CapturedRequest:
    return cap(f"https://ex.com/api/items?page={n}",
               {"page": n, "items": [{"id": i, **extra} for i in range(3)]})  # fmt: skip


def test_generate_records_versions_and_diff_reports_changes(tmp_path: Path) -> None:
    db, out = tmp_path / "g.db", tmp_path / "out"
    save_run(db, [items(1), items(2), items(3)])
    first = runner.invoke(app, ["--json", "generate", "ex.com", "--no-llm", "--db", str(db),
                                "--out", str(out)])  # fmt: skip
    assert first.exit_code == 0, first.output
    package = out / "ex.com" / "package" / "python"
    assert 'version = "1.0.0"' in (package / "pyproject.toml").read_text(encoding="utf-8")
    assert "First version." in (package / "CHANGELOG.md").read_text(encoding="utf-8")

    same = json.loads(runner.invoke(app, ["--json", "diff", "ex.com", "--db", str(db)]).stdout)
    assert same["changes"] == [] and same["version"] == "1.0.0"

    # The site adds a field and a new endpoint: additive, so 1.1.0.
    save_run(db, [items(1, name="n"), items(2, name="n"), items(3, name="n"),
                  cap("https://ex.com/api/stats", {"count": 3})])  # fmt: skip
    changed = runner.invoke(app, ["--json", "diff", "ex.com", "--fail-on", "breaking",
                                  "--db", str(db)])  # fmt: skip
    assert changed.exit_code == 0, changed.output
    data = json.loads(changed.stdout)
    assert data["version"] == "1.1.0" and data["breaking"] == 0
    assert any("new method" in c["text"] for c in data["changes"])
    assert runner.invoke(app, ["diff", "ex.com", "--fail-on", "any", "--db", str(db)]).exit_code \
        == exitcodes.FAILED  # fmt: skip

    markdown = runner.invoke(app, ["diff", "ex.com", "--markdown", "--db", str(db)]).stdout
    assert "## API changes on ex.com" in markdown and "1.0.0 -> 1.1.0" in markdown

    runner.invoke(app, ["generate", "ex.com", "--no-llm", "--db", str(db), "--out", str(out)])
    text = (package / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.index("## 1.1.0") < text.index("## 1.0.0")
    history = json.loads(
        runner.invoke(app, ["--json", "diff", "ex.com", "--history", "--db", str(db)]).stdout
    )
    assert [v["version"] for v in history["versions"]] == ["1.1.0", "1.0.0"]
    since = runner.invoke(app, ["--json", "diff", "ex.com", "--since", "1.0.0", "--db", str(db)])
    data = json.loads(since.stdout)
    assert data["previous"] == "1.0.0" and data["version"] == "1.1.0" and data["changes"]
    unknown = runner.invoke(app, ["diff", "ex.com", "--since", "9.9.9", "--db", str(db)])
    assert unknown.exit_code == exitcodes.USAGE


def test_forget_also_removes_versions(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    save_run(db, [items(1), items(2)])
    runner.invoke(app, ["generate", "ex.com", "--no-llm", "--db", str(db), "--out",
                        str(tmp_path / "out")])  # fmt: skip
    runner.invoke(app, ["forget", "ex.com", "--yes", "--db", str(db), "--out",
                        str(tmp_path / "out")])  # fmt: skip
    with Store(db) as store:
        assert store.api_versions("ex.com") == []


def test_diff_live_calls_the_site_and_spots_a_changed_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals import demo_shop

    server = demo_shop.serve(0)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    db = tmp_path / "g.db"
    try:
        run = runner.invoke(app, ["run", url, "-g", "products", "--no-agent", "--no-verify",
                                  "--db", str(db), "--out", str(tmp_path / "out")])  # fmt: skip
        assert run.exit_code == 0, run.output
        calm = json.loads(
            runner.invoke(app, ["--json", "diff", "127.0.0.1", "--live", "--db", str(db)]).stdout
        )
        assert calm["breaking"] == 0, calm
        # The site now sends prices as text: code using the client would break.
        changed = [{**p, "price": str(p["price"])} for p in demo_shop.PRODUCTS]
        monkeypatch.setattr(demo_shop, "PRODUCTS", changed)
        result = runner.invoke(app, ["--json", "diff", "127.0.0.1", "--live", "--fail-on",
                                     "breaking", "--db", str(db)])  # fmt: skip
        assert result.exit_code == exitcodes.FAILED
        data = json.loads(result.stdout)
        assert any("price" in c["where"] and c["severity"] == "breaking" for c in data["changes"])
    finally:
        server.shutdown()


class RenamingLLM:
    """Always suggests new names for everything it is shown."""

    def __init__(self) -> None:
        self.shown: list[list[str]] = []

    def ask(self, messages: list[dict[str, str]], response_model: object = None,
            temperature: float = 0.0) -> object:  # fmt: skip
        from ghost_api.generator.naming import EndpointNames, NamingPlan
        from ghost_api.llm.gateway import LLMReply

        prompt = messages[-1]["content"]
        keys = [line.removeprefix("- key: ") for line in prompt.splitlines()
                if line.startswith("- key: ")]  # fmt: skip
        self.shown.append(keys)
        names = [EndpointNames(key=k, method_name=f"fancy_{i}", response_model=f"Fancy{i}",
                               summary="x") for i, k in enumerate(keys)]  # fmt: skip
        return LLMReply("", "fake", "m", False, NamingPlan(client_class="Fancy", endpoints=names))


def test_a_released_client_keeps_its_names(tmp_path: Path) -> None:
    from ghost_api.generator import plan_for, site_spec

    db = tmp_path / "g.db"
    save_run(db, [items(1), items(2)])
    runner.invoke(app, ["generate", "ex.com", "--no-llm", "--db", str(db), "--out",
                        str(tmp_path / "out")])  # fmt: skip
    save_run(db, [items(1), items(2), cap("https://ex.com/api/stats", {"count": 3})])
    with Store(db) as store:
        before = {m.name for m in site_spec(store, "ex.com").methods}
        llm = RenamingLLM()
        plan_for(store, llm, "ex.com", store.current_endpoints("ex.com"))  # type: ignore[arg-type]
        after = {m.name for m in site_spec(store, "ex.com").methods}
    assert "get_items" in before and "get_items" in after  # not renamed
    assert after - before == {"fancy_0"}  # only the new endpoint got a new name
    assert site_spec_class(db) == "ExClient"


def site_spec_class(db: Path) -> str:
    from ghost_api.generator import site_spec

    with Store(db) as store:
        return site_spec(store, "ex.com").class_name
