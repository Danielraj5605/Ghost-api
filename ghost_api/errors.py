"""Turn common low-level errors into plain explanations with a fix (NFR-23)."""

from __future__ import annotations

import re

# (pattern in the error text, explanation and fix). The order matches ERROR_CODES.
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
        # Only the browser's own timeouts: an LLM or robots.txt timeout is not the website.
        re.compile(r"Timeout \d+ms exceeded|net::ERR_(CONNECTION_)?TIMED_OUT", re.I),
        "The website took too long to respond. Try again, or check that it loads in a browser.",
    ),
    (
        re.compile(r"ERR_CERT_|CERTIFICATE_VERIFY_FAILED", re.I),
        "The website's security certificate is not valid.",
    ),
    (
        re.compile(r"file is not a database|database disk image is malformed", re.I),
        "The database file is damaged or is not a Ghost API database. Move it aside "
        "(or use --db to pick another file) and Ghost API will start a fresh one.",
    ),
    (
        re.compile(r"database is locked", re.I),
        "The database is busy, probably because another ghost-api command is running. "
        "Wait for it to finish, or use --db to pick another file.",
    ),
]


# Stable codes for the explanations above, in the same order: for logs, scripts and bug
# reports, so nothing depends on the wording of a message.
ERROR_CODES = (
    "browser-missing",
    "dns-failed",
    "connection-refused",
    "unsafe-port",
    "offline",
    "site-timeout",
    "bad-certificate",
    "database-damaged",
    "database-locked",
)


def error_code(exc: BaseException) -> str:
    """A stable short code for `exc` (see ERROR_CODES), 'network-denied',
    'response-too-large', or 'unexpected'."""
    from ghost_api.netguard import PolicyDenied, ResponseTooLarge

    chain = _chain(exc)
    if any(isinstance(e, PolicyDenied) for e in chain):
        return "network-denied"
    if any(isinstance(e, ResponseTooLarge) for e in chain):
        return "response-too-large"
    for error in chain:
        for (pattern, _), code in zip(_KNOWN, ERROR_CODES, strict=True):
            if pattern.search(str(error)):
                return code
    return "unexpected"


def _chain(exc: BaseException) -> list[BaseException]:
    chain = [exc]
    while chain[-1].__cause__ is not None and len(chain) < 5:
        chain.append(chain[-1].__cause__)
    return chain


def friendly_error(exc: BaseException) -> str:
    """A short, human explanation of `exc`; falls back to its first line, plus the
    underlying cause when there is one, so the real reason is not lost."""
    for error in _chain(exc):
        for pattern, message in _KNOWN:
            if pattern.search(str(error)):
                return message
    text = _first_line(exc)
    cause = exc.__cause__
    if cause is not None and _first_line(cause) not in text:
        text += f" (cause: {_first_line(cause)})"
    return text[:300]


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
