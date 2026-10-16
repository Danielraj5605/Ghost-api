"""Every test runs without the developer's real LLM keys, .env files or home folder.

Without this, a test that runs a CLI command would load the real .env (keys included)
and could call a real LLM provider: slow, flaky and a cost to the developer.
"""

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest

PROVIDER_KEYS = ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY")


@pytest.fixture(autouse=True)
def isolated_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GHOST_API_DB", raising=False)
    monkeypatch.delenv("GHOST_API_PROVIDERS", raising=False)
    monkeypatch.setenv("GHOST_API_NO_DOTENV", "1")
    monkeypatch.setenv("GHOST_API_HOME", str(tmp_path_factory.mktemp("ghost_home")))
    monkeypatch.setattr(socket, "getaddrinfo", _local_only)
    yield


_real_getaddrinfo = socket.getaddrinfo
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", ""}


def _local_only(host: Any, *args: Any, **kwargs: Any) -> Any:
    """Tests use local servers and fakes only: a lookup of a real host name fails, so a
    test that would quietly use the internet shows up instead of passing by luck."""
    name = host.decode() if isinstance(host, bytes) else str(host or "")
    try:
        ipaddress.ip_address(name.strip("[]"))
        is_ip = True
    except ValueError:
        is_ip = False
    if not is_ip and name.lower() not in LOCAL_NAMES:
        raise socket.gaierror(socket.EAI_NONAME, f"tests may not resolve {name}")
    return _real_getaddrinfo(host, *args, **kwargs)


def pytest_configure(config: pytest.Config) -> None:
    """Subprocesses started by tests (python -m ghost_api ...) inherit these too."""
    import os

    del config
    os.environ["GHOST_API_NO_DOTENV"] = "1"
    for name in PROVIDER_KEYS:
        os.environ.pop(name, None)
