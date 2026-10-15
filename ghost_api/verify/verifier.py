"""Run the generated tests, diagnose failures, repair, and try again (Stage 7).

Each round: generate the client -> run its tests -> for each failing method, classify
the failure and apply one repair. Stops when everything passes, nothing more can be
done, or after MAX_ROUNDS (FR-34, FR-35, FR-36). Every endpoint ends up as:

    verified    its tests passed against the live API
    unverified  not tested, or failed for a reason that may be temporary
                (rate limit, network, missing credential)
    failed      still failing after repairs (login needed, wrong path, bad model)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from ghost_api.analysis.analyzer import analyze, endpoint_keys
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.generator import GenerationResult, generate_from
from ghost_api.generator.naming import NamingPlan
from ghost_api.generator.spec import ClientSpec
from ghost_api.memory.store import Store
from ghost_api.probe.prober import Prober
from ghost_api.probe.replay import Replayer, ReplayRefused, is_replayable, with_overrides
from ghost_api.verify.classify import FailureKind, classify, short_reason, validation_locations
from ghost_api.verify.repair import relax_schema
from ghost_api.verify.runner import TestOutcome, run_node_tests, run_tests

MAX_ROUNDS = 3
BACKOFF_S = 20.0
Status = Literal["verified", "unverified", "failed"]
# Failures that are probably temporary or outside our control.
SOFT_FAILURES: set[FailureKind] = {"rate-limited", "network", "missing-credential", "unknown"}


@dataclass
class Repair:
    method: str
    kind: FailureKind
    action: str  # what was done, in plain words


@dataclass
class Round:
    number: int
    outcomes: list[TestOutcome]
    repairs: list[Repair] = field(default_factory=list)


@dataclass
class VerifyReport:
    rounds: list[Round]
    statuses: dict[str, tuple[Status, str]]  # Endpoint.key -> (status, note)
    result: GenerationResult | None


class Verifier:
    def __init__(
        self,
        domain: str,
        start_url: str,
        endpoints: list[Endpoint],
        requests: list[CapturedRequest],
        out_root: Path,
        raw: list[RawRequest | None] | None = None,
        plan: NamingPlan | None = None,
        replayer: Replayer | None = None,
        explore: Callable[[str], None] | None = None,
        on_round: Callable[[Round], None] | None = None,
        add_sample: Callable[[CapturedRequest, RawRequest], object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        test_runner: Callable[[Path, dict[str, str]], list[TestOutcome]] | None = None,
        lang: str = "python",
        store: Store | None = None,
    ) -> None:
        """`store`: only read, so regenerated packages carry the right client version."""
        self.store = store
        self.domain = domain
        self.start_url = start_url
        self.endpoints = endpoints
        self.requests = requests  # shared with the recorder: new samples land here
        self.raw: list[RawRequest | None] = raw if raw is not None else [None] * len(requests)
        self.out_root = out_root
        self.plan = plan
        self.replayer = replayer
        self.explore = explore
        self.on_round = on_round
        self.add_sample = add_sample  # e.g. TrafficRecorder.add, which also saves it
        self.sleep = sleep
        self.lang = lang
        self.run_tests = test_runner or (run_node_tests if lang == "typescript" else run_tests)
        self._tried: set[tuple[str, str]] = set()  # (endpoint key, repair) already applied

    # --- main loop ---

    def run(self) -> VerifyReport:
        rounds: list[Round] = []
        result: GenerationResult | None = None
        for number in range(1, MAX_ROUNDS + 1):
            result = generate_from(
                self.domain, self.start_url, self.endpoints, self.requests,
                self.out_root, self.plan, self.lang, store=self.store,
            )  # fmt: skip
            outcomes = self.run_tests(result.client.tests_path, self._credentials_env(result.spec))
            current = Round(number, outcomes)
            rounds.append(current)
            failing = _failures_by_method(outcomes)
            if failing and number < MAX_ROUNDS:
                for method, message in failing.items():
                    repair = self._repair(result.spec, method, message)
                    if repair is not None:
                        current.repairs.append(repair)
            if self.on_round:
                self.on_round(current)
            if not failing or not current.repairs:
                break
        return VerifyReport(rounds, self._statuses(rounds, result), result)

    # --- repairs ---

    def _repair(self, spec: ClientSpec, method: str, message: str) -> Repair | None:
        method_spec = next((m for m in spec.methods if m.name == method), None)
        if method_spec is None:
            return None
        ep = self._endpoint(method_spec.endpoint_key)
        if ep is None:
            return None
        kind = classify(message)

        if kind == "schema":
            if self._once(ep, "more-samples") and self._collect_samples(ep):
                return Repair(method, kind, "collected fresh samples to widen the model")
            locations = validation_locations(message)
            if locations and self._once(ep, f"relax:{locations}"):
                ep.response_schema = relax_schema(ep.response_schema, locations)
                fields = ", ".join(".".join(p for p in loc if not p.isdigit()) for loc in locations)
                return Repair(method, kind, f"relaxed fields that did not match: {fields}")
            return None

        if kind == "auth":
            if self._once(ep, "reprobe") and self._reprobe(ep):
                return Repair(method, kind, "re-tested which headers are required")
            if self.explore is not None and self._once(ep, "explore"):
                self.explore(
                    f"Revisit the pages that load {ep.label} so a fresh session is captured"
                )
                return Repair(method, kind, "revisited the site for a fresh session")
            return None

        if kind == "not-found":
            if self._once(ep, "other-example") and self._rotate_examples(ep):
                return Repair(method, kind, "retrying with a different captured example")
            return None

        if kind in ("rate-limited", "network") and self._once(ep, "backoff"):
            self.sleep(BACKOFF_S)
            return Repair(method, kind, f"waited {BACKOFF_S:.0f}s before retrying")
        return None

    def _once(self, ep: Endpoint, repair: str) -> bool:
        key = (ep.key, repair)
        if key in self._tried:
            return False
        self._tried.add(key)
        return True

    def _samples(self, ep: Endpoint) -> list[RawRequest]:
        keys = endpoint_keys(self.requests)
        return [r for k, r in zip(keys, self.raw, strict=False) if k == ep.key and r is not None]

    def _add(self, captured: CapturedRequest, raw: RawRequest) -> None:
        if self.add_sample is not None:
            self.add_sample(captured, raw)  # appends to the shared lists itself
        else:
            self.requests.append(captured)
            self.raw.append(raw)

    def _collect_samples(self, ep: Endpoint) -> bool:
        """Replay the endpoint (and its next page) to get fresh responses, then re-analyze."""
        if self.replayer is None:
            return False
        sample = next((s for s in reversed(self._samples(ep)) if is_replayable(s)), None)
        if sample is None:
            return False
        probes = [sample]
        page = ep.pagination or {}
        if page.get("location") == "query" and page.get("style") in ("page", "offset"):
            current = dict(parse_qsl(urlsplit(sample.url).query)).get(page["param"], "1")
            if current.lstrip("-").isdigit():
                probes.append(with_overrides(sample, query={page["param"]: str(int(current) + 1)}))
        added = 0
        for request in probes:
            try:
                result = self.replayer.send(request)
            except ReplayRefused:
                break
            if result.ok and result.captured is not None:
                self._add(result.captured, request)
                added += 1
        if added:
            self._reanalyze()
        return added > 0

    def _reprobe(self, ep: Endpoint) -> bool:
        if self.replayer is None:
            return False
        report = Prober(self.replayer, on_sample=self._add).probe(ep, self._samples(ep))
        if not report.replayable:
            return False
        ep.required_headers = report.required_headers
        ep.auth_type = report.auth_type
        return True

    def _rotate_examples(self, ep: Endpoint) -> bool:
        changed = False
        for p in ep.path_params:
            if len(p.examples) > 1:
                p.examples = p.examples[1:] + p.examples[:1]
                changed = True
        return changed

    def _reanalyze(self) -> None:
        """Rebuild endpoints from all samples, keeping what probing learned."""
        old = {e.key: e for e in self.endpoints}
        fresh = []
        for ep in analyze(self.requests):
            prev = old.get(ep.key)
            if prev is None:
                continue  # verification only repairs known endpoints
            ep.replayable, ep.auth_type = prev.replayable, prev.auth_type
            ep.pagination, ep.required_headers = prev.pagination, prev.required_headers
            fresh.append(ep)
        self.endpoints[:] = fresh

    def _endpoint(self, key: str) -> Endpoint | None:
        return next((e for e in self.endpoints if e.key == key), None)

    # --- credentials and results ---

    def _credentials_env(self, spec: ClientSpec) -> dict[str, str]:
        """Credential values from the captured originals, for the test process only."""
        env: dict[str, str] = {}
        for cred in spec.credentials:
            for raw in reversed([r for r in self.raw if r is not None]):
                value = None
                if cred.location == "query":
                    value = dict(parse_qsl(urlsplit(raw.url).query)).get(cred.wire)
                else:
                    value = next(
                        (v for k, v in raw.headers.items() if k.lower() == cred.wire.lower()),
                        None,
                    )
                if value:
                    env[cred.env] = value
                    break
        return env

    def _statuses(
        self, rounds: list[Round], result: GenerationResult | None
    ) -> dict[str, tuple[Status, str]]:
        statuses: dict[str, tuple[Status, str]] = {}
        if result is None or not rounds:
            return statuses
        last = rounds[-1].outcomes
        for m in result.spec.methods:
            mine = [o for o in last if o.method == m.name]
            failed = [o for o in mine if o.outcome == "failed"]
            if failed:
                kind = classify(failed[0].message)
                status: Status = "unverified" if kind in SOFT_FAILURES else "failed"
                statuses[m.endpoint_key] = (status, short_reason(kind, failed[0].message))
            elif mine and all(o.outcome == "passed" for o in mine):
                statuses[m.endpoint_key] = ("verified", f"{len(mine)} live test(s) passed")
            else:
                reason = next((o.message for o in mine if o.outcome == "skipped"), "not tested")
                statuses[m.endpoint_key] = ("unverified", reason or "not tested")
        return statuses


def _failures_by_method(outcomes: list[TestOutcome]) -> dict[str, str]:
    failing: dict[str, str] = {}
    for o in outcomes:
        if o.outcome == "failed" and o.method and o.method not in failing:
            failing[o.method] = o.message
    return failing
