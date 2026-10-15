"""`ghost-api forget`: delete what is stored about a site (SECURITY.md, data retention)."""

from pathlib import Path

from typer.testing import CliRunner

from ghost_api.analysis.analyzer import analyze
from ghost_api.capture.models import CapturedRequest
from ghost_api.cli import app
from ghost_api.memory.store import Store
from ghost_api.pipeline import screenshots_dir

runner = CliRunner()


def cap(url: str) -> CapturedRequest:
    return CapturedRequest("GET", url, 200, "fetch", {}, None, None, "application/json", '{"a": 1}')


def seed(db: Path, site: str) -> int:
    with Store(db) as store:
        run = store.create_run(site, f"https://{site}/", "items")
        request = cap(f"https://{site}/api/items")
        store.add_request(run, request)
        store.save_endpoints(site, run, analyze([request]))
        store.add_lessons(site, run, ["scroll to load more"], "auto")
        store.allow_endpoints(site, [f"GET {site}/api/items"])
        store.cache_put("k", "p", "m", "cached answer")
    return run


def test_forget_removes_one_site_and_its_files(tmp_path: Path) -> None:
    db = tmp_path / "ghost_api.db"
    gone_run = seed(db, "a.com")
    seed(db, "b.com")
    out = tmp_path / "output"
    (out / "a.com").mkdir(parents=True)
    (out / "a.com" / "report.html").write_text("x", encoding="utf-8")
    (out / "b.com").mkdir()
    shots = screenshots_dir(db, gone_run)
    shots.mkdir(parents=True)

    result = runner.invoke(app, ["forget", "a.com", "--yes", "--db", str(db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    with Store(db) as store:
        assert {r.domain for r in store.list_runs()} == {"b.com"}
        assert store.load_endpoints("a.com") == []
        assert store.lessons_for("a.com") == []
        assert store.requests_for_domain("a.com") == []
        assert len(store.load_endpoints("b.com")) == 1
        assert store.cache_get("k") == "cached answer"  # only with --llm-cache
    assert not (out / "a.com").exists() and (out / "b.com").exists()
    assert not shots.exists()


def test_forget_asks_first_and_can_clear_the_llm_cache(tmp_path: Path) -> None:
    db = tmp_path / "ghost_api.db"
    seed(db, "a.com")
    refused = runner.invoke(app, ["forget", "a.com", "--db", str(db)], input="n\n")
    assert refused.exit_code == 1
    with Store(db) as store:
        assert store.list_runs()
    runner.invoke(app, ["forget", "--llm-cache", "--yes", "--db", str(db)])
    with Store(db) as store:
        assert store.cache_get("k") is None and store.list_runs()
