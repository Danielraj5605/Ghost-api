"""Phase 1: project settings (ghost-api.toml), init, --json output and exit codes."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ghost_api import exitcodes
from ghost_api.analysis.analyzer import analyze
from ghost_api.capture.models import CapturedRequest
from ghost_api.cli import _json_first, app
from ghost_api.config import default_db_path, load_env
from ghost_api.memory.store import Store
from ghost_api.project import (
    PROJECT_FILE,
    ProjectError,
    default_map,
    ensure_gitignore,
    find_project,
    load_project,
    write_project,
)

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("GHOST_API_DB", raising=False)
    monkeypatch.delenv("GHOST_API_NO_PROJECT", raising=False)
    write_project(tmp_path, "typescript")
    (tmp_path / "deep" / "er").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "deep" / "er")
    return tmp_path


def seed(db: Path, site: str = "ex.com") -> None:
    req = CapturedRequest(
        "GET", f"https://{site}/api/items", 200, "fetch", {}, None, None, "application/json",
        '{"a": 1}',
    )  # fmt: skip
    with Store(db) as store:
        run = store.create_run(site, f"https://{site}/", "items")
        store.add_request(run, req)
        store.save_endpoints(site, run, analyze([req]))


# --- the project file ---------------------------------------------------------------------


def test_project_is_found_from_a_subfolder(project: Path) -> None:
    found = find_project()
    assert found is not None and found.root == project.resolve()
    assert found.database == project.resolve() / ".ghost-api" / "ghost_api.db"
    assert found.defaults["lang"] == "typescript"
    assert default_db_path() == found.database


def test_ghost_api_db_still_wins(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHOST_API_DB", "other.db")
    assert default_db_path() == Path("other.db")


def test_defaults_reach_the_commands_that_have_those_options(project: Path) -> None:
    import typer

    found = find_project()
    assert found is not None
    mapping = default_map(found, typer.main.get_command(app))
    assert mapping["run"]["lang"] == "typescript"
    assert mapping["run"]["out"] == str(found.output)
    assert "lang" not in mapping.get("doctor", {})  # doctor has no --lang option
    assert mapping["fetch"]["out"] == str(found.output)


def test_project_defaults_are_used_and_options_still_win(project: Path) -> None:
    seed(default_db_path())
    out = project / "output"
    result = runner.invoke(app, ["generate", "ex.com", "--no-llm"])
    assert result.exit_code == 0, result.output
    assert list(out.rglob("*_client.ts"))  # lang = "typescript" from the project file
    result = runner.invoke(app, ["generate", "ex.com", "--no-llm", "--lang", "python"])
    assert result.exit_code == 0, result.output
    assert list(out.rglob("*_client.py"))


def test_a_broken_project_file_is_a_usage_error(project: Path) -> None:
    (project / PROJECT_FILE).write_text("[project\nnot toml", encoding="utf-8")
    with pytest.raises(ProjectError):
        load_project(project / PROJECT_FILE)
    result = runner.invoke(app, ["inspect"])
    assert result.exit_code == exitcodes.USAGE
    assert "can't be read" in result.output


def test_gitignore_keeps_keys_and_data_out(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n.env\n", encoding="utf-8")
    assert ensure_gitignore(tmp_path) == [".ghost-api/", "output/"]
    assert ensure_gitignore(tmp_path) == []
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith("node_modules/") and text.count(".env") == 1


def test_env_from_other_folders_never_moves_the_database(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = project / "home"
    home.mkdir()
    (home / ".env").write_text("GHOST_API_DB=elsewhere.db\nGROQ_API_KEY=from-home\n", "utf-8")
    monkeypatch.setenv("GHOST_API_HOME", str(home))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    load_env()
    assert os.environ.get("GROQ_API_KEY") == "from-home"
    assert "GHOST_API_DB" not in os.environ
    monkeypatch.delenv("GROQ_API_KEY")


# --- init ----------------------------------------------------------------------------------


def test_init_sets_up_a_folder_and_is_safe_to_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    result = runner.invoke(app, ["--json", "init", "--yes", "--skip-browser"])
    assert result.exit_code == 0, result.output
    done = json.loads(result.stdout)
    assert done["project_created"] and done["gitignore_added"]
    assert (tmp_path / PROJECT_FILE).exists() and (tmp_path / ".ghost-api").is_dir()
    again = json.loads(runner.invoke(app, ["--json", "init", "--yes", "--skip-browser"]).stdout)
    assert again["project_created"] is False and again["gitignore_added"] == []


def test_init_saves_a_key_without_printing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("GHOST_API_HOME", str(tmp_path / "home"))
    result = runner.invoke(app, ["init", "--skip-browser"], input="y\n2\nfake-removed\n")
    assert result.exit_code == 0, result.output
    assert "GROQ_API_KEY=fake-removed" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "fake-removed" not in result.output
    monkeypatch.delenv("GROQ_API_KEY")


# --- --json and exit codes ---------------------------------------------------------------


def test_json_flag_can_be_typed_anywhere() -> None:
    assert _json_first(["inspect", "x.com", "--json"]) == ["--json", "inspect", "x.com"]
    assert _json_first(["inspect"]) == ["inspect"]


def test_inspect_json_is_clean_on_stdout(project: Path) -> None:
    seed(default_db_path())
    result = runner.invoke(app, ["--json", "inspect", "ex.com"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["site"] == "ex.com" and data["endpoint_count"] == 1
    assert data["endpoints"][0]["path"] == "/api/items"


def test_generate_json_names_the_client(project: Path) -> None:
    seed(default_db_path())
    data = json.loads(runner.invoke(app, ["--json", "generate", "ex.com", "--no-llm"]).stdout)
    assert data["client"]["lang"] == "typescript" and data["client"]["files"]


def test_robots_refusal_has_its_own_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from ghost_api import cli

    monkeypatch.setattr(cli, "robots_policy", lambda *a: (None, False))
    result = runner.invoke(app, ["--json", "run", "https://ex.com/", "-g", "x", "--no-agent"])
    assert result.exit_code == exitcodes.NOT_ALLOWED
    assert json.loads(result.stdout)["status"] == "not-allowed"


def test_run_statuses_map_to_exit_codes() -> None:
    assert exitcodes.for_run_status("goal-covered") == 0
    assert exitcodes.for_run_status("failed") == exitcodes.FAILED
    assert exitcodes.for_run_status("bot-protection") == exitcodes.NOT_ALLOWED
    assert exitcodes.for_run_status("interrupted") == exitcodes.INTERRUPTED


def test_run_json_has_the_endpoint_list_and_count(tmp_path: Path) -> None:
    from ghost_api.cli import _RunResult
    from ghost_api.events import EndpointsUpdated, RunFinished, RunStarted

    result = _RunResult()
    result(RunStarted(7, "ex.com", "https://ex.com/", "items"))
    result(EndpointsUpdated([{"path": "/api/items"}]))
    result(RunFinished("captured", "done", 1.0, 3, 1))
    assert result.data["run_id"] == 7 and result.data["endpoint_count"] == 1
    assert result.data["endpoints"] == [{"path": "/api/items"}]
