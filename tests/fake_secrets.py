"""Fake secrets for the redaction tests, built at run time.

Secret scanners (GitHub, GitGuardian...) flag token-shaped text in a repository even when
it is test data. Building the values here keeps token-shaped literals out of the source,
while the tests still check exactly the same shapes. None of these values is real.
"""

import base64
import json
from typing import Any


def _part(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def fake_jwt() -> str:
    """header.payload.signature, like a JWT, but signed by nobody."""
    return ".".join([_part({"alg": "HS256"}), _part({"sub": "test-user"}), "x" * 22])


def fake_token() -> str:
    """A credential-looking value to put after 'Bearer '."""
    return "-".join(["fake", "test", "value", "0000"])


def bearer() -> str:
    return " ".join(["Bearer", fake_token()])
