"""Exit codes of the `ghost-api` command, for scripts and CI. Stable across releases."""

from __future__ import annotations

OK = 0  # done (a run may still have found nothing: read the JSON or the report)
FAILED = 1  # the command failed: an error, failed checks, nothing stored to work on
USAGE = 2  # wrong command line or setup: bad option, missing LLM key, bad project file
NOT_ALLOWED = 3  # the site said no: robots.txt disallows it, or a bot-protection page
INTERRUPTED = 130  # stopped with Ctrl+C

_BY_STATUS = {
    "failed": FAILED,
    "llm-unavailable": FAILED,
    "bot-protection": NOT_ALLOWED,
    "interrupted": INTERRUPTED,
}


def for_run_status(status: str) -> int:
    """The exit code for a run or refresh that ended with `status`."""
    return _BY_STATUS.get(status, OK)
