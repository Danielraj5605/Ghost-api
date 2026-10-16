"""Local LLMs (Ollama, LM Studio...): prompts never leave the machine."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ghost_api.llm.gateway import LLMGateway
from ghost_api.llm.providers import default_providers


@pytest.fixture
def local_server() -> Iterator[tuple[str, list[dict[str, object]]]]:
    seen: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "model": body["model"]})
            reply = {"choices": [{"message": {"content": "hello from local"}}],
                     "usage": {"prompt_tokens": 3, "completion_tokens": 2}}  # fmt: skip
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1", seen
    httpd.shutdown()


def test_a_local_model_is_used_alone_by_default(
    local_server: tuple[str, list[dict[str, object]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, seen = local_server
    monkeypatch.setenv("GHOST_API_LOCAL_URL", url)
    monkeypatch.setenv("GHOST_API_LOCAL_MODEL", "qwen3:8b")
    monkeypatch.setenv("GEMINI_API_KEY", "x")  # even with a cloud key present
    providers = default_providers()
    assert [p.name for p in providers] == ["local"]
    gateway = LLMGateway(providers)
    reply = gateway.ask([{"role": "user", "content": "hi"}])
    assert reply.text == "hello from local"
    assert seen == [{"path": "/v1/chat/completions", "model": "qwen3:8b"}]


def test_local_can_be_combined_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHOST_API_LOCAL_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("GHOST_API_PROVIDERS", "local,groq")
    assert [p.name for p in default_providers()] == ["local", "groq"]
    monkeypatch.delenv("GHOST_API_LOCAL_URL")
    with pytest.raises(ValueError, match="GHOST_API_LOCAL_URL"):
        default_providers()
