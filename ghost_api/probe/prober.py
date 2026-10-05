"""Confirm or reject guesses about each endpoint by calling it (Stage 5).

For every endpoint with a replayable sample:
  1. Replay it as captured. If that fails, the endpoint can't be called outside the
     browser (signed URLs, one-time tokens...), and testing stops there.
  2. Pagination: request the next page / offset / cursor and compare the records.
  3. Required headers: drop all non-browser headers at once; only if that breaks the call,
     drop them one at a time to find the ones that matter.
  4. Secret-looking query parameters: drop each to see if it's required (API keys).
  5. Name the auth style from what turned out to be required.

Every test is recorded as a hypothesis with its evidence (FR-24).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from ghost_api.analysis import detectors
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.filters import is_sensitive_name
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.probe.replay import (
    Replayer,
    ReplayRefused,
    ReplayResult,
    is_replayable,
    with_overrides,
)

Outcome = Literal["confirmed", "rejected", "inconclusive"]
MAX_HEADER_TESTS = 10


@dataclass
class Hypothesis:
    endpoint: str  # Endpoint.key
    text: str
    result: Outcome
    evidence: str


@dataclass
class ProbeReport:
    endpoint: str  # Endpoint.key
    replayable: bool = False
    pagination: dict[str, Any] | None = None
    required_headers: list[str] = field(default_factory=list)
    required_query: list[str] = field(default_factory=list)
    auth_type: str | None = None
    hypotheses: list[Hypothesis] = field(default_factory=list)
    note: str = ""


class Prober:
    def __init__(
        self,
        replayer: Replayer,
        on_sample: Callable[[CapturedRequest, RawRequest], object] | None = None,
    ) -> None:
        self.replayer = replayer
        self.on_sample = on_sample  # new samples (e.g. page 2) enrich the schemas

    def probe(self, endpoint: Endpoint, samples: list[RawRequest]) -> ProbeReport:
        report = ProbeReport(endpoint.key)
        sample = next((s for s in reversed(samples) if is_replayable(s)), None)
        if sample is None:
            report.note = "no sample that is safe to replay (only read-only requests are sent)"
            return report
        try:
            baseline = self._baseline(endpoint, sample, report)
            if baseline is None:
                return report
            self._pagination(endpoint, sample, baseline, report)
            self._headers(endpoint, sample, baseline, report)
            self._secret_params(endpoint, sample, baseline, report)
        except ReplayRefused as exc:
            report.note = f"testing stopped: {exc}"
        report.auth_type = detectors.auth_style(report.required_headers, report.required_query)
        if report.auth_type == "bearer":
            auth_value = next(
                (v for k, v in sample.headers.items() if k.lower() == "authorization"), ""
            )
            if auth_value.lower().startswith("basic "):
                report.auth_type = "basic"
        return report

    # --- tests ---

    def _baseline(
        self, ep: Endpoint, sample: RawRequest, report: ProbeReport
    ) -> ReplayResult | None:
        result = self.replayer.send(sample)
        ok = result.ok
        report.replayable = ok
        report.hypotheses.append(
            Hypothesis(
                ep.key,
                "can be called directly, outside the browser",
                "confirmed" if ok else "rejected",
                _evidence(result),
            )
        )
        return result if ok else None

    def _pagination(
        self, ep: Endpoint, sample: RawRequest, baseline: ReplayResult, report: ProbeReport
    ) -> None:
        query = dict(parse_qsl(urlsplit(sample.url).query, keep_blank_values=True))
        body = _json_body(sample)
        guess = detectors.guess_pagination(query, body, baseline.body)
        if guess is None:
            return
        source = query if guess.location == "query" else (body or {})
        current = source.get(guess.param)
        text = f"paginates with {guess.style} parameter '{guess.param}' ({guess.evidence})"

        next_value: str | int | None
        if guess.style == "cursor":
            next_value = detectors.next_cursor(baseline.body)
            if next_value is None:
                report.hypotheses.append(
                    Hypothesis(ep.key, text, "inconclusive", "no next cursor in the response")
                )
                return
        elif guess.style == "page":
            next_value = _as_int(current, default=1) + 1
        else:  # offset
            items = detectors.find_items(baseline.body) or []
            step = _as_int(source.get(guess.size_param) if guess.size_param else None, len(items))
            next_value = _as_int(current, default=0) + max(step, 1)

        if guess.location == "query":
            probe = with_overrides(sample, query={guess.param: str(next_value)})
        else:
            typed = next_value if isinstance(current, int) else str(next_value)
            probe = with_overrides(sample, body={guess.param: typed})
        result = self.replayer.send(probe)
        outcome, evidence = _compare_pages(baseline, result)
        report.hypotheses.append(Hypothesis(ep.key, text, outcome, evidence))
        if outcome != "rejected":
            report.pagination = {
                "style": guess.style,
                "param": guess.param,
                "location": guess.location,
                "size_param": guess.size_param,
                "confirmed": outcome == "confirmed",
            }
        if result.ok and result.captured is not None and self.on_sample:
            self.on_sample(result.captured, probe)

    def _headers(
        self, ep: Endpoint, sample: RawRequest, baseline: ReplayResult, report: ProbeReport
    ) -> None:
        candidates = detectors.candidate_headers(sample.headers)[:MAX_HEADER_TESTS]
        if not candidates:
            return
        stripped = self.replayer.send(with_overrides(sample, drop_headers=set(candidates)))
        if _same(baseline, stripped):
            report.hypotheses.append(
                Hypothesis(
                    ep.key,
                    "needs none of its custom headers or cookies",
                    "confirmed",
                    f"still works without {', '.join(candidates)}",
                )  # fmt: skip
            )
            return
        for header in candidates:
            result = self.replayer.send(with_overrides(sample, drop_headers={header}))
            required = not _same(baseline, result)
            if required:
                report.required_headers.append(header)
            report.hypotheses.append(
                Hypothesis(
                    ep.key,
                    f"header '{header}' is required",
                    "confirmed" if required else "rejected",
                    f"without it: {_evidence(result)}",
                )
            )

    def _secret_params(
        self, ep: Endpoint, sample: RawRequest, baseline: ReplayResult, report: ProbeReport
    ) -> None:
        query = dict(parse_qsl(urlsplit(sample.url).query, keep_blank_values=True))
        for name in [k for k in query if is_sensitive_name(k)]:
            result = self.replayer.send(with_overrides(sample, query={name: None}))
            required = not _same(baseline, result)
            if required:
                report.required_query.append(name)
            report.hypotheses.append(
                Hypothesis(
                    ep.key,
                    f"query parameter '{name}' (a key or token) is required",
                    "confirmed" if required else "rejected",
                    f"without it: {_evidence(result)}",
                )
            )


# --- helpers ---------------------------------------------------------------------------------


def _json_body(raw: RawRequest) -> dict[str, Any] | None:
    if not raw.body:
        return None
    try:
        data = json.loads(raw.body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _same(baseline: ReplayResult, other: ReplayResult) -> bool:
    return other.ok and detectors.shape(other.body) == detectors.shape(baseline.body)


def _evidence(result: ReplayResult) -> str:
    if result.error and result.status == 0:
        return result.error
    text = f"HTTP {result.status}"
    if result.error:
        text += f", {result.error}"
    elif result.ok:
        items = detectors.find_items(result.body)
        if items is not None:
            text += f", {len(items)} records"
    return text


def _compare_pages(first: ReplayResult, second: ReplayResult) -> tuple[Outcome, str]:
    if not second.ok:
        return "inconclusive", f"next page failed: {_evidence(second)}"
    if detectors.shape(first.body) != detectors.shape(second.body):
        return "inconclusive", "next page returned a different kind of response"
    a = detectors.item_fingerprints(detectors.find_items(first.body) or [])
    b = detectors.item_fingerprints(detectors.find_items(second.body) or [])
    if not a or not b:
        return "inconclusive", f"pages had {len(a)} and {len(b)} records"
    overlap = len(set(a) & set(b)) / max(len(set(b)), 1)
    if overlap == 1.0:
        return "rejected", "the next page returned the same records"
    if overlap < 0.5:
        return "confirmed", f"the next page returned {len(b)} different records"
    return "inconclusive", f"{overlap:.0%} of records repeated on the next page"
