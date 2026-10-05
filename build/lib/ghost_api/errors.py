"""Turn common low-level errors into plain explanations with a fix (NFR-23)."""

from __future__ import annotations

import re

# (pattern in the error text, explanation and fix)
_KNOWN: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"Executable doesn't exist|playwright install", re.I),
        "Chromium isn't installed for Playwright. Run: playwright install chromium",
    ),
    (
        re.compile(r"ERR_NAME_NOT_RESOLVED|getaddrinfo failed|Name or service not known", re.I),
        "The website's address could not be found. Check the URL and your internet connection.",
    ),
    (
        re.compile(r"ERR_CONNECTION_REFUSED|Connection refused|actively refused", re.I),
        "The website refused the connection. Is the URL (and port) right, and is the site up?",
    ),
    (
        re.compile(r"ERR_UNSAFE_PORT", re.I),
        "Browsers refuse to open this port for safety reasons. Use a different port.",
    ),
    (
        re.compile(r"ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED", re.I),
        "No internet connection.",
    ),
    (
        re.compile(r"Timeout \d+ms exceeded|ERR_TIMED_OUT|timed out", re.I),
        "The website took too long to respond. Try again, or check that it loads in a browser.",
    ),
    (
        re.compile(r"ERR_CERT_|certificate", re.I),
        "The website's security certificate is not valid.",
    ),
    (
        re.compile(r"database is locked", re.I),
        "The database is busy, probably because another ghost-api command is running. "
        "Wait for it to finish, or use --db to pick another file.",
    ),
]


def friendly_error(exc: BaseException) -> str:
    """A short, human explanation of `exc`; falls back to its first line."""
    text = str(exc)
    for pattern, message in _KNOWN:
        if pattern.search(text):
            return message
    first = text.strip().splitlines()[0] if text.strip() else type(exc).__name__
    return first[:200]
