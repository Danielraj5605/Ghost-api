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


def fake_value(label: str) -> str:
    """A placeholder credential (an API key, a password, a session id) for tests, built
    from parts so no credential-like literal sits in the source: fake-<label>-0000."""
    return "-".join(["fake", label, "0000"])


API_KEY = fake_value("apikey")
PASSWORD = fake_value("password")
SESSION_ID = fake_value("session")
TOKEN = fake_value("token")


def fake_provider_keys() -> list[str]:
    """Values shaped like real secret API keys (OpenAI, Anthropic, GitHub, AWS, Slack,
    Stripe, Groq), assembled from a prefix and filler so none appears in the source."""
    body = "Ab1" * 12  # mixed case and digits, like a random key
    return [
        "sk" + "-" + body,
        "sk" + "-ant-api03-" + body,
        "gh" + "p_" + body,
        "github" + "_pat_" + body,
        "AK" + "IA" + "ABCDEFGH23456789",
        "xo" + "xb-" + "1234567890-" + body,
        "sk" + "_live_" + body,
        "gs" + "k_" + body,
    ]
