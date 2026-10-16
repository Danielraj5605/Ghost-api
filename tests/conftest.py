"""Every test runs without the developer's real LLM keys, .env files or home folder.

Without this, a test that runs a CLI command would load the real .env (keys included)
and could call a real LLM provider: slow, flaky and a cost to the developer.
"""

from collections.abc import Iterator

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
    yield


def pytest_configure(config: pytest.Config) -> None:
    """Subprocesses started by tests (python -m ghost_api ...) inherit these too."""
    import os

    del config
    os.environ["GHOST_API_NO_DOTENV"] = "1"
    for name in PROVIDER_KEYS:
        os.environ.pop(name, None)
