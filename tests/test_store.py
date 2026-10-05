from pathlib import Path

from ghost_api.capture.models import CapturedRequest
from ghost_api.memory.store import Store


def test_run_and_request_roundtrip(tmp_path: Path) -> None:
    with Store(tmp_path / "t.db") as store:
        run_id = store.create_run("example.com", "https://example.com", "list books")
        req = CapturedRequest(
            method="GET",
            url="https://example.com/api/books?page=1",
            status=200,
            resource_type="fetch",
            req_headers={"accept": "application/json"},
            req_body=None,
            req_content_type=None,
            resp_content_type="application/json",
            resp_body='{"books": []}',
        )
        store.add_request(run_id, req)
        store.finish_run(run_id, "captured")

        assert store.requests_for_run(run_id) == [req]
        runs = store.list_runs("example.com")
        assert len(runs) == 1
        assert runs[0].status == "captured"
        assert runs[0].request_count == 1
        assert store.list_runs("other.com") == []
