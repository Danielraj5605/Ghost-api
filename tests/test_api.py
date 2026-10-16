"""The public Python API (ghost_api.api): the same workflows as the CLI, as functions."""

from pathlib import Path

import pytest

from ghost_api import api
from ghost_api.events import Event


def test_the_python_api_end_to_end(tmp_path: Path) -> None:
    from evals import demo_shop

    server = demo_shop.serve(0)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    db, out = tmp_path / "g.db", tmp_path / "out"
    events: list[Event] = []
    try:
        result = api.discover(url, "list products", use_agent=False, db=db, out=out,
                              verify=False, on_event=events.append)  # fmt: skip
        assert result.ok and result.site == "127.0.0.1" and result.run_id is not None
        assert {e["path"] for e in result.endpoints} >= {"/api/categories", "/api/products"}
        assert any(f.endswith("_client.py") for f in result.client_files)
        assert result.network["direct_calls"] >= 1 and Path(result.report).exists()
        assert any(e.kind == "run_finished" for e in events)

        assert api.sites(db=db) == ["127.0.0.1"]
        ops = {o["name"]: o for o in api.operations("127.0.0.1", db=db)}
        assert "get_products" in ops
        rows = api.fetch("127.0.0.1", "get_products", pages=2, db=db)
        assert len(rows) >= 6 and "price" in rows[0]

        client = api.generate("127.0.0.1", db=db, out=out)
        assert client["version"] == "1.0.0" and client["class_name"].endswith("Client")
        assert api.diff("127.0.0.1", db=db)["changes"] == []

        with pytest.raises(api.GhostApiError):
            api.fetch("127.0.0.1", "no_such_operation", db=db)
        with pytest.raises(api.GhostApiError):
            api.generate("never-explored.example", db=db, out=out)
    finally:
        server.shutdown()


def test_without_a_key_the_agent_is_refused_clearly(tmp_path: Path) -> None:
    from evals import demo_shop

    server = demo_shop.serve(0)
    try:
        with pytest.raises(api.NoLLMKey):
            api.discover(f"http://127.0.0.1:{server.server_address[1]}/", "x",
                         db=tmp_path / "g.db", out=tmp_path / "out")  # fmt: skip
    finally:
        server.shutdown()
