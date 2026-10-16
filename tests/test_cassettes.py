"""Phase B: generated tests replay recorded replies offline, and run live on request."""

import json
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from ghost_api.cli import app
from ghost_api.generator.cassettes import path_regex

runner = CliRunner()


def run_generated_tests(
    folder: Path, test_file: str, live: bool
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "GHOST_API_LIVE"}
    if live:
        env["GHOST_API_LIVE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_file],
        cwd=folder, env=env, capture_output=True, text=True, timeout=300,
    )  # fmt: skip


def test_generated_tests_pass_offline_after_the_site_is_gone(tmp_path: Path) -> None:
    from evals.demo_shop import serve

    shop = serve(0)
    url = f"http://127.0.0.1:{shop.server_address[1]}/"
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", url, "-g", "products", "--no-agent", "--no-verify",
                                 "--db", str(tmp_path / "g.db"), "--out", str(out)])  # fmt: skip
    assert result.exit_code == 0, result.output
    shop.shutdown()  # from here on, nothing answers at that address
    shop.server_close()

    folder = out / "127.0.0.1"
    cassettes = json.loads((folder / "cassettes.json").read_text(encoding="utf-8"))
    assert {r["path"] for r in cassettes["replies"]} >= {"/api/products", "/api/categories"}
    test_file = next(p.name for p in folder.glob("test_*.py"))

    offline = run_generated_tests(folder, test_file, live=False)
    assert offline.returncode == 0, offline.stdout + offline.stderr
    assert "passed" in offline.stdout and "failed" not in offline.stdout

    live = run_generated_tests(folder, test_file, live=True)
    assert live.returncode != 0  # the live API is down: live mode really goes live

    package_tests = folder / "package" / "python" / "tests"
    assert (package_tests / "cassettes.json").exists()


def test_paths_with_parameters_match_any_value() -> None:
    import re

    pattern = path_regex("/api/items/{item_id}/reviews")
    assert re.fullmatch(pattern, "/api/items/42/reviews")
    assert not re.fullmatch(pattern, "/api/items/42/other")


def test_typescript_tests_replay_offline_too(tmp_path: Path) -> None:
    import shutil

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    from evals.demo_shop import serve

    shop = serve(0)
    url = f"http://127.0.0.1:{shop.server_address[1]}/"
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", url, "-g", "products", "--no-agent", "--no-verify",
                                 "--lang", "typescript", "--db", str(tmp_path / "g.db"),
                                 "--out", str(out)])  # fmt: skip
    assert result.exit_code == 0, result.output
    shop.shutdown()
    shop.server_close()
    folder = out / "127.0.0.1"
    test_file = next(p.name for p in folder.glob("*.test.ts"))
    env = {k: v for k, v in os.environ.items() if k != "GHOST_API_LIVE"}
    offline = subprocess.run([node, "--test", test_file], cwd=folder, env=env,
                             capture_output=True, text=True, timeout=300)  # fmt: skip
    assert offline.returncode == 0, offline.stdout + offline.stderr
    assert "fail 0" in offline.stdout and "pass 0" not in offline.stdout
