"""Phase 2: generated clients come as installable packages with a README."""

import importlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from ghost_api.generator.package import curl_example, example_value
from ghost_api.generator.python_gen import write
from ghost_api.generator.ts_gen import write_typescript
from tests.test_generator import spec_for

NPX = shutil.which("npx")
NODE = shutil.which("node")


def test_python_package_builds_and_imports_like_the_loose_files(tmp_path: Path) -> None:
    result = write(spec_for(), tmp_path)
    root = tmp_path / "package" / "python"
    src = root / "src" / result.module
    assert {p.name for p in src.iterdir()} >= {"__init__.py", "client.py", "models.py", "py.typed"}
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "unofficial" in readme and "curl" in readme and "ShopTestNotFoundError" in readme

    wheel_dir = tmp_path / "wheel"
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "-q", "-w", str(wheel_dir)],
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    wheel = next(wheel_dir.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert f"{result.module}/py.typed" in names and f"{result.module}/client.py" in names

    sys.path.insert(0, str(root / "src"))
    try:
        package = importlib.import_module(result.module)
        assert package.ShopTestClient.__name__ == "ShopTestClient"
        assert issubclass(package.ShopTestNotFoundError, package.ShopTestError)
        assert hasattr(package, "AsyncShopTestClient")
    finally:
        sys.path.remove(str(root / "src"))
        for name in [n for n in sys.modules if n.startswith(result.module)]:
            sys.modules.pop(name)


@pytest.mark.skipif(importlib.util.find_spec("mypy") is None, reason="mypy not installed")
def test_python_package_passes_mypy_strict(tmp_path: Path) -> None:
    result = write(spec_for(), tmp_path)
    src = tmp_path / "package" / "python" / "src"
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", "-p", result.module],
        cwd=src, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(NPX is None or NODE is None, reason="Node.js is not installed")
def test_typescript_package_compiles_and_runs(tmp_path: Path) -> None:
    from tests.test_typescript import spec_for as ts_spec

    write_typescript(ts_spec("http://127.0.0.1:1"), tmp_path)
    root = tmp_path / "package" / "typescript"
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert package["types"] == "./dist/index.d.ts" and package["engines"]["node"] == ">=18"
    proc = subprocess.run(
        [NPX or "npx", "--yes", "--package", "typescript@5", "tsc", "-p", "."],
        cwd=root, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    if proc.returncode != 0 and "npm" in proc.stderr and "ERR" in proc.stderr:
        pytest.skip(f"could not fetch the TypeScript compiler: {proc.stderr[-200:]}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (root / "dist" / "index.d.ts").exists()
    script = root / "check.mjs"
    script.write_text(
        """import * as client from "./dist/index.js";
const names = Object.keys(client).filter((n) => n.endsWith("Error")).sort();
const notFound = names.find((n) => n.endsWith("NotFoundError"));
const base = names.find((n) => !/(Auth|NotFound|RateLimit|Server)Error$/.test(n));
const err = new client[notFound](404, "Not Found");
console.log(JSON.stringify({ names, isBase: err instanceof client[base], name: err.name }));
""",
        encoding="utf-8",
    )
    run = subprocess.run(
        [NODE or "node", script.name], cwd=root, capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert len(out["names"]) == 5 and out["isBase"] and out["name"].endswith("NotFoundError")


def test_curl_examples_use_real_values_and_never_secrets() -> None:
    spec = spec_for()
    by_name = {m.name: m for m in spec.methods}
    item = curl_example(spec, by_name["get_item"])
    assert item.startswith("curl ") and "/api/items/" in item and "{" not in item
    search = curl_example(spec, by_name["post_search"])
    assert "-X POST" in search and "Content-Type: application/json" in search
    for method in spec.methods:
        assert "fake-removed" not in curl_example(spec, method).replace("API_KEY", "")
    assert example_value("'dune'") == "dune" and example_value("12") == 12
