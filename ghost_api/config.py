"""Settings and default budgets, loaded from the environment and CLI options."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from ghost_api import __version__

TOOL_NAME = "GhostAPI"
# Appended to the browser's real user-agent so sites can identify the tool (NFR-20).
PROJECT_URL = "https://github.com/Danielraj5605/Ghost-api"
USER_AGENT_SUFFIX = f"{TOOL_NAME}/{__version__} (API discovery tool; +{PROJECT_URL})"

SUPPORTED_LANGUAGES = ("python", "typescript")


def check_language(lang: str) -> str:
    """The one place the client language is validated (CLI options and Settings use it)."""
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"Unsupported language '{lang}'. Supported: {', '.join(SUPPORTED_LANGUAGES)}"
        )
    return lang


def tool_user_agent() -> str:
    """The user-agent for requests Ghost API (not an LLM provider) sends to a target site.

    Some sites (Wikimedia's, for one) refuse clients that don't say who runs them. If
    GHOST_API_CONTACT is set (an email or a URL, chosen by the user), it is added. It is
    never filled in automatically.
    """
    contact = os.getenv("GHOST_API_CONTACT", "").strip()
    return f"{USER_AGENT_SUFFIX} (contact: {contact})" if contact else USER_AGENT_SUFFIX


@dataclass
class Settings:
    """Everything a run needs. CLI options override the defaults below."""

    url: str
    goal: str
    lang: str = "python"
    headed: bool = False
    max_steps: int = 40
    max_llm_calls: int = 50
    max_minutes: float = 15.0
    rate_limit: float = 1.0  # max requests per second the tool itself sends
    ignore_robots: bool = False
    db_path: Path = Path("ghost_api.db")
    output_dir: Path = Path("output")

    def __post_init__(self) -> None:
        check_language(self.lang)
        if self.rate_limit <= 0:
            raise ValueError("--rate-limit must be greater than 0")
        for name in ("max_steps", "max_llm_calls"):
            if getattr(self, name) < 1:
                raise ValueError(f"--{name.replace('_', '-')} must be at least 1")


@dataclass(frozen=True)
class ApiKeys:
    gemini: str | None
    groq: str | None
    openrouter: str | None


def load_env() -> None:
    """Load .env from the current directory (if present) without overriding real env vars."""
    load_dotenv(override=False)


def state_dir() -> Path:
    """Small files Ghost API keeps between runs (history, provider cooldowns)."""
    return Path(os.getenv("GHOST_API_HOME") or Path.home() / ".ghost-api")


def default_db_path() -> Path:
    return Path(os.getenv("GHOST_API_DB", "ghost_api.db"))


def api_keys() -> ApiKeys:
    return ApiKeys(
        gemini=os.getenv("GEMINI_API_KEY") or None,
        groq=os.getenv("GROQ_API_KEY") or None,
        openrouter=os.getenv("OPENROUTER_API_KEY") or None,
    )
