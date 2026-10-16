"""Phase D: per-site project rules (ghost-api.toml) and the audit log."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ghost_api import exitcodes
from ghost_api.cli import app
from ghost_api.project import ProjectError, load_project

runner = CliRunner()


def project(root: Path, rules: str) -> None:
    (root / "ghost-api.toml").write_text(
        '[project]\ndatabase = "g.db"\noutput = "out"\n' + rules, encoding="utf-8"
    )


@pytest.fixture
def in_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_rules_are_read_and_checked(tmp_path: Path) -> None:
    project(tmp_path, 'require_terms = true\n[sites."ex.com"]\nterms_ok = true\n'
            'deny_paths = ["/admin"]\nmax_direct_calls = 5\nagent = false\n')  # fmt: skip
    loaded = load_project(tmp_path / "ghost-api.toml")
    policy = loaded.policy("ex.com/shop")  # a section falls back to its host's rules
    assert loaded.require_terms and policy.terms_ok and not policy.agent
    assert policy.denies("/admin") and policy.denies("/admin/users")
    assert not policy.denies("/administrator") and policy.max_direct_calls == 5
    project(tmp_path, '[sites."ex.com"]\ndeny_paths = ["admin"]\n')
    with pytest.raises(ProjectError, match="paths like"):
        load_project(tmp_path / "ghost-api.toml")


def test_terms_must_be_checked_first_when_the_project_says_so(in_project: Path) -> None:
    from evals.fixtures import serve

    site = serve("offset-paging")
    try:
        project(in_project, "require_terms = true\n")
        blocked = runner.invoke(app, ["--json", "run", site.url, "-g", "items", "--no-agent"])
        assert blocked.exit_code == exitcodes.NOT_ALLOWED
        assert "terms_ok = true" in json.loads(blocked.stdout)["reason"]
        assert site.hits == []  # nothing was sent before the check

        project(in_project, 'require_terms = true\n[sites."127.0.0.1"]\nterms_ok = true\n'
                "agent = false\n")  # fmt: skip
        no_ai = runner.invoke(app, ["run", site.url, "-g", "items"])  # the agent is asked for
        assert no_ai.exit_code == exitcodes.NOT_ALLOWED and "no AI explores" in no_ai.output
        allowed = runner.invoke(app, ["run", site.url, "-g", "items", "--no-agent",
                                      "--no-verify"])  # fmt: skip
        assert allowed.exit_code == 0, allowed.output
    finally:
        site.shutdown()


def test_denied_paths_are_never_requested_and_everything_is_audited(in_project: Path) -> None:
    from evals.fixtures import serve

    site = serve("huge-reply")  # its page calls /api/huge and /api/small
    try:
        project(in_project, '[sites."127.0.0.1"]\ndeny_paths = ["/api/huge"]\n')
        result = runner.invoke(app, ["--json", "run", site.url, "-g", "items", "--no-agent",
                                     "--no-verify"])  # fmt: skip
        assert result.exit_code == 0, result.output
        assert not site.hit("/api/huge")  # not by the browser, not by a direct call
        assert site.hit("/api/small")
        network = json.loads(result.stdout)["network"]
        assert any("/api/huge" in b for b in network["blocked"])

        audited = json.loads(runner.invoke(app, ["--json", "audit", "127.0.0.1"]).stdout)
        entries = audited["entries"]
        sent = [e for e in entries if e.get("decision") == "sent"]
        assert sent and all(e["path"] == "/api/small" and e["status"] == 200 for e in sent)
        assert any(e["kind"] == "blocked" and "/api/huge" in e["decision"] for e in entries)
        assert all(set(e) <= {"at", "site", "run", "kind", "method", "host", "path", "query",
                              "status", "decision"} for e in entries)  # fmt: skip

        forgot = runner.invoke(app, ["forget", "127.0.0.1", "--yes"])
        assert forgot.exit_code == 0 and "audit log" in forgot.output
    finally:
        site.shutdown()


def test_an_assistant_cannot_explore_a_site_the_rules_close(in_project: Path) -> None:
    from evals.fixtures import serve
    from ghost_api.drive import DriveSession
    from ghost_api.workflows import PolicyBlocked

    site = serve("offset-paging")
    try:
        project(in_project, '[sites."127.0.0.1"]\nagent = false\n')
        with pytest.raises(PolicyBlocked):
            DriveSession(site.url, "items", db=in_project / "g.db").start()
        assert site.hits == []
    finally:
        site.shutdown()
