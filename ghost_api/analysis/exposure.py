"""Which endpoints expose personal data, from what was hidden at capture time.

Each stored request carries the personal data Ghost API hid from it
(`CapturedRequest.personal`, see capture/personal.py). Grouped by endpoint, that says
which endpoints hand out emails, phone numbers, PAN/GST, bank details... and whether
they do it without a login. Values are never stored: only field paths, kinds, counts
and masked examples.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ghost_api.analysis.analyzer import endpoint_keys
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.models import CapturedRequest
from ghost_api.capture.personal import LABEL

SEVERITY_ORDER = {"high": 0, "medium": 1}


@dataclass
class Exposure:
    endpoint_key: str
    label: str
    open: bool  # served without a login (no auth found, callable directly or untested)
    fields: list[dict[str, Any]] = field(default_factory=list)  # path, kind, severity, ...

    @property
    def severity(self) -> str:
        return str(min((f["severity"] for f in self.fields), key=SEVERITY_ORDER.__getitem__))

    @property
    def kinds(self) -> list[str]:
        return sorted({LABEL.get(f["kind"], f["kind"]) for f in self.fields})

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint_key,
            "label": self.label,
            "open": self.open,
            "severity": self.severity,
            "kinds": self.kinds,
            "fields": self.fields,
        }


def exposures(endpoints: list[Endpoint], requests: list[CapturedRequest]) -> list[Exposure]:
    """Endpoints whose replies contained personal data, most serious first."""
    by_key: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for key, req in zip(endpoint_keys(requests), requests, strict=False):
        for found in req.personal:
            entry = by_key.setdefault(key, {}).setdefault(
                (found["path"], found["kind"]), {**found, "count": 0}
            )
            entry["count"] += int(found.get("count", 1))
    result = []
    for ep in endpoints:
        fields = by_key.get(ep.key)
        if not fields:
            continue
        ordered = sorted(fields.values(), key=lambda f: (SEVERITY_ORDER[f["severity"]], f["path"]))
        result.append(Exposure(ep.key, ep.label, is_open(ep), ordered))
    return sorted(result, key=lambda e: (not e.open, SEVERITY_ORDER[e.severity], e.label))


def is_open(ep: Endpoint) -> bool:
    """No login involved: no auth style was found and it wasn't shown to need one."""
    return ep.auth_type in (None, "none") and ep.replayable is not False


def summary_line(found: list[Exposure]) -> str:
    """'2 endpoints expose personal data without a login (PAN number, email address)'."""
    open_ones = [e for e in found if e.open]
    kinds = sorted({k for e in (open_ones or found) for k in e.kinds})
    n = len(open_ones) or len(found)
    where = "without a login" if open_ones else "(a login is involved)"
    noun = "endpoint exposes" if n == 1 else "endpoints expose"
    return f"{n} {noun} personal data {where}: {', '.join(kinds)}"
