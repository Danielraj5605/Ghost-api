"""Sites: a host, or host/section, so one big host's apps don't share memory and files.

Asked for after a run on https://www.google.com/maps was filed under www.google.com,
where a later run on www.google.com/search would have mixed with it.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ghost_api.analysis.analyzer import analyze
from ghost_api.capture.models import CapturedRequest
from ghost_api.events import EventBus
from ghost_api.export import write_exports
from ghost_api.export.site_data import endpoint_summary
from ghost_api.generator.naming import default_client_name
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.pipeline import open_session
from ghost_api.site import SiteAmbiguous, is_site, resolve_site, site_host, site_key
from ghost_api.web.server import RunManager, create_app


@pytest.mark.parametrize(
    ("url", "site"),
    [
        ("https://www.google.com/maps", "www.google.com/maps"),
        ("https://www.google.com/maps/@12.9,77.6,15z", "www.google.com/maps"),
        ("https://www.google.com/search?q=x", "www.google.com/search"),
        ("https://quotes.toscrape.com/scroll", "quotes.toscrape.com/scroll"),
        ("https://hn.algolia.com/", "hn.algolia.com"),
        ("https://hn.algolia.com", "hn.algolia.com"),
        ("https://books.toscrape.com/index.html", "books.toscrape.com"),  # a file
        ("https://example.com/en-us/shop", "example.com"),  # a locale
        ("https://example.com/123/items", "example.com"),  # an ID
        ("https://example.com/pb=!1m8!3m7", "example.com"),  # encoded data
        ("http://127.0.0.1:8800/", "127.0.0.1"),
    ],
)
def test_site_key(url: str, site: str) -> None:
    assert site_key(url) == site
    assert is_site(site)


def test_unsafe_site_names_are_refused() -> None:
    for name in ("../etc", "a.com/../x", "a.com/b/c", ".hidden", "a.com/", "", "a b.com"):
        assert not is_site(name), name


def test_host_and_names_for_a_site() -> None:
    assert site_host("www.google.com/maps") == "www.google.com"
    assert default_client_name("www.google.com/maps") == "GoogleMapsClient"
    assert default_client_name("hn.algolia.com") == "HnAlgoliaClient"
    # First-party is still decided by the host.
    ep = analyze([cap("https://www.google.com/maps/preview/pegman")])[0]
    summary = endpoint_summary(ep, "www.google.com/maps")
    assert summary["third_party"] is False and summary["label"] == "GET /maps/preview/pegman"


def test_resolve_site() -> None:
    known = ["www.google.com/maps", "www.google.com/search", "hn.algolia.com", "q.com/scroll"]
    assert resolve_site("www.google.com/maps", known) == "www.google.com/maps"
    assert resolve_site("https://www.google.com/maps/@1,2", known) == "www.google.com/maps"
    assert resolve_site("q.com", known) == "q.com/scroll"  # the only site under it
    assert resolve_site("hn.algolia.com/", known) == "hn.algolia.com"
    assert resolve_site("never.seen", known) == "never.seen"
    with pytest.raises(SiteAmbiguous) as err:
        resolve_site("www.google.com", known)
    assert err.value.choices == ["www.google.com/maps", "www.google.com/search"]


def cap(url: str) -> CapturedRequest:
    return CapturedRequest(
        "GET", url, 200, "fetch", {}, None, None, "application/json", '{"ok": true}'
    )


def stored_site(store: Store, url: str) -> int:
    """What a run leaves behind: a run, its requests, endpoints and exports."""
    bus = EventBus()
    host = site_key(url).split("/")[0]
    s = open_session(
        store, bus, host, url, "g", False, RateLimiter(50), RobotsPolicy.allow_all(host), None, 5
    )
    req = cap(f"https://{host}/api/thing")
    store.add_request(s.run_id, req)
    store.save_endpoints(s.site, s.run_id, analyze([req]))
    store.finish_run(s.run_id, "captured")
    return s.run_id


def test_runs_are_stored_and_written_per_site(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with Store(tmp_path / "t.db") as store:
        maps = stored_site(store, "https://www.google.com/maps")
        search = stored_site(store, "https://www.google.com/search?q=x")
        assert store.get_run(maps).domain == "www.google.com/maps"  # type: ignore[union-attr]
        assert store.get_run(search).domain == "www.google.com/search"  # type: ignore[union-attr]
        maps_files = write_exports(store, "www.google.com/maps", maps, out)
        write_exports(store, "www.google.com/search", search, out)
    assert maps_files.report == out / "www.google.com" / "maps" / "report.md"
    assert (out / "www.google.com" / "search" / "report.md").exists()


def test_dashboard_uses_tilde_for_sections(tmp_path: Path) -> None:
    db, out = tmp_path / "t.db", tmp_path / "out"
    with Store(db) as store:
        run_id = stored_site(store, "https://www.google.com/maps")
        write_exports(store, "www.google.com/maps", run_id, out)
    client = TestClient(
        create_app(db, out, "t", manager=RunManager(db, out, gateway_factory=lambda c: None))
    )
    auth = {"x-ghost-token": "t"}
    sites = client.get("/api/sites", headers=auth).json()
    assert [s["domain"] for s in sites] == ["www.google.com/maps"]
    detail = client.get("/api/sites/www.google.com~maps", headers=auth)
    assert detail.status_code == 200 and detail.json()["domain"] == "www.google.com/maps"
    files = {f["name"] for f in detail.json()["files"]}
    assert "report.md" in files
    report = client.get("/api/sites/www.google.com~maps/files/report.md", headers=auth)
    assert report.text.startswith("# Ghost API report: www.google.com/maps")
    zipped = client.get("/api/sites/www.google.com~maps/download.zip?token=t")
    assert 'filename="www.google.com-maps.zip"' in zipped.headers["content-disposition"]
    for bad in ("..~etc", "www.google.com~..", "a~b~c"):
        assert client.get(f"/api/sites/{bad}", headers=auth).status_code in (400, 404)


def test_cli_accepts_the_domain_when_it_has_one_site(tmp_path: Path) -> None:
    from ghost_api.cli import app

    db = tmp_path / "t.db"
    with Store(db) as store:
        stored_site(store, "https://www.google.com/maps")
        stored_site(store, "https://www.google.com/search")
        stored_site(store, "https://quotes.example/scroll")
    runner = CliRunner()
    out = str(tmp_path / "o")
    ok = runner.invoke(app, ["report", "quotes.example", "--db", str(db), "--out", out])
    assert ok.exit_code == 0, ok.output
    assert (tmp_path / "o" / "quotes.example" / "scroll" / "report.md").exists()
    ambiguous = runner.invoke(app, ["report", "www.google.com", "--db", str(db), "--out", out])
    assert ambiguous.exit_code == 1
    assert "www.google.com/maps" in ambiguous.output and "www.google.com/search" in ambiguous.output
