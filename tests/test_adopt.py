"""`ghost-api adopt`: find hand-written calls to hidden APIs and their client replacements."""

import json
from pathlib import Path

from typer.testing import CliRunner

from ghost_api.adopt import scan
from ghost_api.cli import app

runner = CliRunner()


def codebase(root: Path, base: str) -> None:
    (root / "app").mkdir()
    (root / "app" / "shop.py").write_text(
        "import requests, httpx\n"
        f"r = requests.get('{base}/api/products?page=2')\n"
        f'c = httpx.post("{base}/api/cart", json={{}})\n',
        encoding="utf-8",
    )
    (root / "web").mkdir()
    (root / "web" / "page.ts").write_text(
        f"const a = await fetch('{base}/api/products/12/reviews');\n"
        f"const b = await fetch(`{base}/api/search?q=x`, {{ method: 'GET' }});\n"
        "const c = await axios.get('https://api.other-site.example/v1/things');\n"
        "const d = fetch('/relative/path');\n",
        encoding="utf-8",
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "lib.js").write_text(
        "fetch('https://ignored.example/x')", encoding="utf-8"
    )


def test_scan_finds_literal_url_calls_and_skips_vendored_code(tmp_path: Path) -> None:
    codebase(tmp_path, "https://shop.example")
    calls = {(c.file.replace("\\", "/"), c.method, c.url) for c in scan(tmp_path)}
    assert ("app/shop.py", "GET", "https://shop.example/api/products?page=2") in calls
    assert ("app/shop.py", "POST", "https://shop.example/api/cart") in calls
    assert ("web/page.ts", "GET", "https://api.other-site.example/v1/things") in calls
    assert not any("ignored.example" in c[2] or "relative" in c[2] for c in calls)


def test_adopt_maps_calls_to_client_methods(tmp_path: Path) -> None:
    from evals.demo_shop import serve

    shop = serve(0)
    host = f"127.0.0.1:{shop.server_address[1]}"
    db = tmp_path / "g.db"
    try:
        result = runner.invoke(app, ["run", f"http://{host}/", "-g", "products", "--no-agent",
                                     "--no-verify", "--db", str(db),
                                     "--out", str(tmp_path / "out")])  # fmt: skip
        assert result.exit_code == 0, result.output
    finally:
        shop.shutdown()
    # The codebase calls the shop by a host name Ghost API knows as 127.0.0.1.
    project = tmp_path / "project"
    project.mkdir()
    codebase(project, f"http://{host}")
    adopted = runner.invoke(app, ["--json", "adopt", str(project), "--db", str(db)])
    assert adopted.exit_code == 0, adopted.output
    hosts = {h["host"]: h for h in json.loads(adopted.stdout)["hosts"]}
    other = hosts["api.other-site.example"]
    assert (
        other["site"] is None
        and "ghost-api run https://api.other-site.example" in (other["next_step"])
    )
    # 127.0.0.1 is skipped as local: what matters is that known hosts are matched.
    assert "127.0.0.1" not in hosts


def test_known_public_host_gets_replacements(tmp_path: Path) -> None:
    from ghost_api import adopt as adopt_module
    from ghost_api.analysis.analyzer import analyze
    from ghost_api.capture.models import CapturedRequest
    from ghost_api.memory.store import Store

    db = tmp_path / "g.db"
    reqs = [
        CapturedRequest("GET", f"https://shop.example/api/products?page={p}", 200, "fetch",
                        {}, None, None, "application/json",
                        json.dumps({"page": p, "products": [{"id": 1}]}))
        for p in (1, 2, 3)
    ]  # fmt: skip
    with Store(db) as store:
        run = store.create_run("shop.example", "https://shop.example/", "products")
        for r in reqs:
            store.add_request(run, r)
        store.save_endpoints("shop.example", run, analyze(reqs))
        project = tmp_path / "project"
        project.mkdir()
        codebase(project, "https://shop.example")
        reports = {r.host: r for r in adopt_module.adopt(project, store)}
    shop = reports["shop.example"]
    assert shop.site == "shop.example" and shop.client_class == "ShopExampleClient"
    mapped = {c.url: c.replacement for c in shop.calls}
    assert mapped["https://shop.example/api/products?page=2"] == "api.get_products()"
    assert mapped["https://shop.example/api/cart"] is None  # never explored (and a POST)
    assert "ghost-api generate shop.example" in shop.next_step
