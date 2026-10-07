"""Personal data: found, hidden before storing, and reported (usefulness phase 2).

Prompted by turftown.in, whose public venue details returned owners' PAN and GST numbers,
UPI IDs and staff emails. All values below are fake and built at run time.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ghost_api.analysis.analyzer import analyze
from ghost_api.analysis.exposure import exposures, summary_line
from ghost_api.capture.personal import classify, hide, hide_body, mask
from ghost_api.export import write_exports
from ghost_api.export.site_data import endpoint_details
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.probe.replay import Replayer
from ghost_api.runtime import SiteRuntime
from tests.test_google_maps_findings import FakeResponse, record

EMAIL = "owner" + "@" + "example.com"
PAN = "ABCDE" + "1234" + "F"
GSTIN = "29" + PAN + "1Z5"
UPI = "venue.owner" + "@" + "okaxis"
PHONE = 9_876_543_210


def venue(i: int) -> dict[str, object]:
    return {
        "id": i,
        "name": f"Venue {i}",
        "price": 100 + i,
        "created_by": EMAIL,
        "tax_info": {"pan_number": PAN, "gst_number": GSTIN},
        "bank": {"upi_id": UPI},
        "contacts": [{"phone": PHONE, "name": "Desk"}],
    }


@pytest.mark.parametrize(
    ("key", "value", "kind"),
    [
        ("created_by", EMAIL, "email"),
        ("pan_number", PAN, "pan"),
        ("gst", GSTIN, "gstin"),
        ("handle", UPI, "upi"),
        ("phone", PHONE, "phone"),
        ("mobile", "+91 98765 43210", "phone"),
        ("aadhaar", "2345 6789 0123", "aadhaar"),
        ("account_number", "123456789012", "bank_account"),
        ("dob", "1990-01-31", "date_of_birth"),
        # Not personal: IDs, prices, pincodes, versions, URLs, plain numbers.
        ("id", PHONE, None),
        ("pincode", "600042", None),
        ("version", "v1.2@beta", None),
        ("website", "https://example.com", None),
        ("name", "Tiki Taka", None),
        ("price", 214, None),
        ("is_open", True, None),
    ],
)
def test_classify(key: str, value: object, kind: str | None) -> None:
    assert classify(key, value) == kind


def test_mask_keeps_the_kind_not_the_value() -> None:
    assert mask(EMAIL, "email") == "o***@e***.com"
    masked = mask(PAN, "pan")
    assert masked.startswith("AB") and masked.endswith("F") and "1234" not in masked


def test_hide_replaces_values_and_keeps_types() -> None:
    data, found = hide({"data": venue(1)})
    text = json.dumps(data)
    for secret in (EMAIL, PAN, GSTIN, UPI, str(PHONE)):
        assert secret not in text
    assert data["data"]["created_by"] == "[hidden email: o***@e***.com]"
    assert data["data"]["contacts"][0]["phone"] == 0  # numbers stay numbers
    assert data["data"]["price"] == 101 and data["data"]["name"] == "Venue 1"
    assert {f.path for f in found} == {
        "data.created_by", "data.tax_info.pan_number", "data.tax_info.gst_number",
        "data.bank.upi_id", "data.contacts[].phone",
    }  # fmt: skip
    # Already-hidden markers are not found again.
    assert hide(data)[1] == []


def test_recorder_never_stores_the_values() -> None:
    body = json.dumps({"venues": [venue(1), venue(2)]}).encode()
    rec = record(FakeResponse("https://www.google.com/maps/api/venues", body))
    (captured,) = rec.captured
    assert PAN not in (captured.resp_body or "") and EMAIL not in (captured.resp_body or "")
    kinds = {(p["path"], p["kind"], p["count"]) for p in captured.personal}
    assert ("venues[].tax_info.pan_number", "pan", 2) in kinds
    assert ("venues[].created_by", "email", 2) in kinds
    assert hide_body("not json") == ("not json", [])


# --- end to end: stored, reported, hidden in runtime output ---


class VenueHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        data = json.dumps({"data": venue(int(self.path.rsplit("/", 1)[1]))}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def venue_server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), VenueHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def stored_venue_run(store: Store, base: str) -> int:
    replayer = Replayer("127.0.0.1", RateLimiter(10_000), RobotsPolicy.allow_all("127.0.0.1"))
    from ghost_api.capture.models import RawRequest

    run_id = store.create_run("127.0.0.1", f"{base}/", "venues")
    captured = []
    for i in (1, 2):
        result = replayer.send(RawRequest("GET", f"{base}/api/venue/{i}", {}, None))
        assert result.captured is not None
        captured.append(result.captured)
        store.add_request(run_id, result.captured)
    replayer.close()
    store.save_endpoints("127.0.0.1", run_id, analyze(captured))
    store.finish_run(run_id, "captured")
    return run_id


def test_reported_but_never_kept(tmp_path: Path, venue_server: str) -> None:
    out = tmp_path / "out"
    with Store(tmp_path / "t.db") as store:
        run_id = stored_venue_run(store, venue_server)
        stored = store.requests_for_domain("127.0.0.1")
        found = exposures(store.load_endpoints("127.0.0.1"), stored)
        details = endpoint_details(store, "127.0.0.1")
        exports = write_exports(store, "127.0.0.1", run_id, out)
    # Nothing personal in the database...
    db_text = (tmp_path / "t.db").read_bytes()
    for secret in (EMAIL, PAN, GSTIN, UPI):
        assert secret.encode() not in db_text
    # ...but the exposure is known, per endpoint, with masked examples.
    (exp,) = found
    assert exp.open and exp.severity == "high" and exp.label == "GET /api/venue/{id}"
    assert "PAN number" in exp.kinds and "email address" in exp.kinds
    assert summary_line(found).startswith("1 endpoint exposes personal data without a login")
    assert details[0]["exposure"]["fields"][0]["severity"] == "high"
    report = exports.report.read_text(encoding="utf-8")
    assert "## Personal data exposed" in report and "data.tax_info.pan_number" in report
    for secret in (EMAIL, PAN, GSTIN, UPI):
        assert secret not in report
        assert secret not in (exports.html.read_text(encoding="utf-8") if exports.html else "")


def test_runtime_hides_unless_asked(tmp_path: Path, venue_server: str) -> None:
    with Store(tmp_path / "t.db") as store:
        stored_venue_run(store, venue_server)
        fast = Replayer("127.0.0.1", RateLimiter(10_000), RobotsPolicy.allow_all("127.0.0.1"))
        rt = SiteRuntime(store, "127.0.0.1", fast)
        audit = SiteRuntime(store, "127.0.0.1", fast, include_sensitive=True)
    op = rt.operations()[0]
    hidden = rt.call(op, {"id": "7"})
    assert hidden.body["data"]["name"] == "Venue 7"  # type: ignore[index]
    assert PAN not in json.dumps(hidden.body) and hidden.hidden
    raw = audit.call(op, {"id": "7"})
    assert raw.body["data"]["tax_info"]["pan_number"] == PAN  # type: ignore[index]
    rt.close()
