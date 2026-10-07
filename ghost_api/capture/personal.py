"""Find personal and sensitive data in API replies, and hide it before anything is kept.

Found on turftown.in: a public venue-details call returned owners' PAN and GST numbers,
bank UPI IDs and staff emails to anyone. Ghost API now spots such values when a reply is
captured, replaces them with a marker like "[hidden email: a***@g***.com]" (strings) or
0 (numbers, so the inferred type stays the same), and records what it hid. The report
then shows which endpoints expose what, without ever storing the values themselves.

Detection uses the value's shape and the field's name together, to keep false alarms
low: a 10-digit number is only a phone number in a field called phone/mobile/contact.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

Severity = Literal["high", "medium"]

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_UPI_RE = re.compile(r"^[A-Za-z0-9._-]{2,}@[A-Za-z][A-Za-z0-9]{1,}$")  # name@okaxis (no dot)
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{7,16}[0-9]$")
_INTL_PHONE_RE = re.compile(r"^\+[1-9][0-9 ()-]{7,16}[0-9]$")
_AADHAAR_RE = re.compile(r"^[2-9][0-9]{3}[ -]?[0-9]{4}[ -]?[0-9]{4}$")
_DIGITS_RE = re.compile(r"^[0-9]{9,19}$")
_MARKER_RE = re.compile(r"^\[hidden ([a-z_]+): (.*)\]$")

_PHONE_NAMES = ("phone", "mobile", "contact", "whatsapp", "msisdn", "tel")
_ACCOUNT_NAMES = ("account_number", "accountnumber", "account_no", "acc_no", "bank_account")
_CARD_NAMES = ("card", "cc_number", "ccnumber")
_AADHAAR_NAMES = ("aadhaar", "aadhar", "uid_number")
_UPI_NAMES = ("upi", "vpa")
# Common UPI handle providers (the part after "@"), for values in fields not named upi/vpa.
_UPI_PROVIDERS = {
    "okaxis", "okhdfcbank", "okicici", "oksbi", "ybl", "ibl", "axl", "paytm", "upi", "apl",
    "yapl", "rapl", "airtel", "freecharge", "jio", "kotak", "hdfcbank", "icici", "sbi",
    "axisbank", "pthdfc", "ptsbi", "ptyes", "ptaxis", "waaxis", "wahdfcbank", "wasbi",
    "waicici", "abfspay", "ikwik", "idfcbank", "indus", "barodampay", "fbl", "federal",
}  # fmt: skip
_DOB_NAMES = ("dob", "date_of_birth", "dateofbirth", "birth_date", "birthdate", "birthday")
# A person's name, when the field says so (names can't be told from their value alone).
_HOLDER_NAMES = (
    "accountname", "account_name", "accountholder", "account_holder", "holder_name",
    "holdername", "beneficiary", "owner_name", "ownername", "card_holder", "cardholder",
)  # fmt: skip

SEVERITY: dict[str, Severity] = {
    "pan": "high", "gstin": "high", "aadhaar": "high", "bank_account": "high",
    "card": "high", "upi": "high", "email": "medium", "phone": "medium",
    "date_of_birth": "medium", "account_holder": "medium",
}  # fmt: skip
LABEL = {
    "pan": "PAN number", "gstin": "GST number", "aadhaar": "Aadhaar number",
    "bank_account": "bank account number", "card": "card number", "upi": "UPI ID",
    "email": "email address", "phone": "phone number", "date_of_birth": "date of birth",
    "account_holder": "account holder's name",
}  # fmt: skip


@dataclass(frozen=True)
class Finding:
    path: str  # field path, list positions as []: "data.rating[].email"
    kind: str
    severity: Severity
    example: str  # masked: "a***@g***.com"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def classify(key: str, value: Any) -> str | None:
    """The kind of personal data `value` (in field `key`) is, or None."""
    if isinstance(value, bool) or value is None:
        return None
    name = key.lower()
    text = str(value).strip()
    if not text or _MARKER_RE.match(text):
        return None
    if isinstance(value, str):
        if _EMAIL_RE.match(text):
            return "email"
        if _PAN_RE.match(text):
            return "pan"
        if _GSTIN_RE.match(text):
            return "gstin"
        if _UPI_RE.match(text) and (
            any(n in name for n in _UPI_NAMES) or text.rpartition("@")[2].lower() in _UPI_PROVIDERS
        ):
            return "upi"
        if _INTL_PHONE_RE.match(text) and _digit_count(text) >= 10:
            return "phone"
    if (
        isinstance(value, str)
        and any(n in name.replace("-", "_") for n in _HOLDER_NAMES)
        and re.search(r"[A-Za-z]{2}", text)
    ):
        return "account_holder"
    if any(n in name for n in _DOB_NAMES) and isinstance(value, str) and re.search(r"\d", text):
        return "date_of_birth"
    if any(n in name for n in _AADHAAR_NAMES) and _AADHAAR_RE.match(text):
        return "aadhaar"
    if any(n in name for n in _ACCOUNT_NAMES) and _DIGITS_RE.match(text):
        return "bank_account"
    if any(n in name for n in _CARD_NAMES) and _DIGITS_RE.match(text) and _luhn(text):
        return "card"
    if (
        any(n in name for n in _PHONE_NAMES)
        and _PHONE_RE.match(text)
        and 10 <= _digit_count(text) <= 15
    ):
        return "phone"
    return None


def mask(text: str, kind: str) -> str:
    """Enough to recognise the kind, nothing to recover the value: 'a***@g***.com'."""
    if kind in ("email", "upi") and "@" in text:
        user, _, domain = text.partition("@")
        head, dot, tld = domain.rpartition(".")
        domain_masked = f"{head[:1]}***{dot}{tld}" if dot else f"{domain[:1]}***"
        return f"{user[:1]}***@{domain_masked}"
    keep = 2 if len(text) > 6 else 1
    return text[:keep] + "*" * max(3, len(text) - keep - 1) + text[-1:]


def hide(data: Any, path: str = "") -> tuple[Any, list[Finding]]:
    """A copy of `data` with personal values replaced, and what was replaced."""
    findings: list[Finding] = []

    def walk(value: Any, key: str, where: str) -> Any:
        if isinstance(value, dict):
            return {
                k: walk(v, str(k), f"{where}.{k}" if where else str(k)) for k, v in value.items()
            }
        if isinstance(value, list):
            return [walk(v, key, f"{where}[]") for v in value]
        kind = classify(key, value)
        if kind is None:
            return value
        example = mask(str(value).strip(), kind)
        findings.append(Finding(where or key or "value", kind, SEVERITY[kind], example))
        if isinstance(value, str):
            return f"[hidden {kind}: {example}]"
        return 0  # numbers stay numbers, so the inferred schema keeps its type

    return walk(data, "", path), findings


def hide_body(body: str | None) -> tuple[str | None, list[dict[str, str]]]:
    """For a JSON body (text): the body with personal values hidden, and what was hidden
    (one entry per field path). Non-JSON bodies are returned as they are."""
    if not body:
        return body, []
    try:
        data = json.loads(body)
    except ValueError:
        return body, []
    hidden, findings = hide(data)
    if not findings:
        return body, []
    return json.dumps(hidden, ensure_ascii=False), summarize(findings)


def summarize(findings: list[Finding]) -> list[dict[str, str]]:
    """One entry per (path, kind), with how many values were hidden."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for f in findings:
        entry = seen.setdefault((f.path, f.kind), {**f.to_dict(), "count": 0})
        entry["count"] += 1
    return list(seen.values())


def _digit_count(text: str) -> int:
    return sum(c.isdigit() for c in text)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0
