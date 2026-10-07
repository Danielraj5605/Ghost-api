"""Run a site's endpoints by name, from stored data: the base for fetch, watch and serve.

A `SiteRuntime` needs no generated code. Each operation replays the endpoint's latest
stored sample with the caller's parameters, through the same `Replayer` the prober uses
(robots.txt, rate limit, read-only rule), follows pages, and turns replies into rows.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ghost_api.analysis.analyzer import endpoint_keys
from ghost_api.analysis.detectors import (
    find_items,
    guess_pagination,
    item_fingerprints,
    next_cursor,
)
from ghost_api.analysis.models import Endpoint
from ghost_api.capture import personal
from ghost_api.capture.filters import is_sensitive_name
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.generator.naming import NamingPlan, apply_plan
from ghost_api.memory.knowledge import rebuild_raw
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter
from ghost_api.probe.replay import Replayer, ReplayResult, apply_params, is_replayable
from ghost_api.site import site_host

MAX_PAGES = 50
MAX_ROWS = 10_000


class OperationError(Exception):
    """A problem the user can act on (unknown operation, needs a fresh sample...)."""


@dataclass
class Input:
    name: str
    location: str  # "path", "query" or "body"
    type: str
    example: str
    required: bool


@dataclass
class Operation:
    name: str
    summary: str
    endpoint: Endpoint
    inputs: list[Input]
    allowed: bool  # the user checked it only reads (a POST search, `generate --allow`)
    pagination: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        return self.endpoint.label


@dataclass
class Page:
    number: int
    result: ReplayResult
    records: list[Any] = field(default_factory=list)


class SiteRuntime:
    def __init__(
        self,
        store: Store,
        site: str,
        replayer: Replayer | None = None,
        rate: float = 1.0,
        include_sensitive: bool = False,
    ) -> None:
        """`include_sensitive`: return personal data (emails, phones, PAN...) as the site
        sends it. Only for auditing your own site; by default such values are hidden."""
        self.site = site
        self.include_sensitive = include_sensitive
        run_id = store.latest_endpoint_run(site)
        if run_id is None:
            raise OperationError(f"Nothing stored for {site}. Run `ghost-api run` first.")
        self.endpoints = store.load_endpoints(site, run_id)
        self.allowed = store.allowed_endpoints(site)
        plan = load_plan(store.load_naming_plan(site))
        _, self.names = apply_plan(plan, self.endpoints, site)
        stored = store.requests_for_domain(site)
        self.samples: dict[str, list[CapturedRequest]] = {}
        for key, req in zip(endpoint_keys(stored), stored, strict=False):
            self.samples.setdefault(key, []).append(req)
        self.replayer = replayer or Replayer(site_host(site), RateLimiter(rate), max_requests=10**6)

    def close(self) -> None:
        self.replayer.close()

    def __enter__(self) -> SiteRuntime:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- what can be called ---

    def operations(self) -> list[Operation]:
        ops = []
        for ep in self.endpoints:
            allowed = ep.key in self.allowed
            if not is_callable(ep, allowed):
                continue  # may change data, or can't be called outside the browser
            names = self.names[ep.key]
            ops.append(
                Operation(
                    names.method_name,
                    names.summary,
                    ep,
                    inputs_for(ep, self.samples.get(ep.key, [])),
                    allowed,
                    ep.pagination or self._guess_pagination(ep),
                )
            )
        return ops

    def operation(self, name: str) -> Operation:
        ops = self.operations()
        for op in ops:
            if name in (op.name, op.endpoint.key, op.label):
                return op
        choices = ", ".join(o.name for o in ops) or "none"
        raise OperationError(f"No operation {name!r} for {self.site}. Choose one of: {choices}")

    # --- calling ---

    def call(self, op: Operation, params: dict[str, str] | None = None) -> ReplayResult:
        sample = self._sample(op)
        values = self._coerce(op, sample, params or {})
        request = apply_params(
            sample, op.endpoint.path_template, {p.name for p in op.endpoint.path_params}, values
        )
        result = self.replayer.send(request, trusted_read=op.allowed)
        if self.include_sensitive or result.body is None:
            return result
        body, hidden = personal.hide(result.body)
        return replace(result, body=body, hidden=personal.summarize(hidden))

    def iterate(
        self,
        op: Operation,
        params: dict[str, str] | None = None,
        max_pages: int = 5,
        max_rows: int = 1000,
    ) -> Iterator[Page]:
        """Pages of records, following the endpoint's pagination; stops when a page is
        empty, repeats what was seen, or a limit is reached."""
        params = dict(params or {})
        pag = op.pagination
        if pag and pag["style"] != "cursor" and pag["param"] not in params:
            params[pag["param"]] = self._first_position(op, pag)  # start at the beginning
        seen: set[str] = set()
        rows = 0
        for number in range(1, min(max_pages, MAX_PAGES) + 1):
            result = self.call(op, params)
            records = records_of(result.body, is_single(op)) if result.ok else []
            fresh = [k for k in item_fingerprints(records) if k not in seen]
            if result.ok and records and not fresh:
                return  # the "next page" repeated the last one: pagination ignored
            if result.ok and not records and number > 1:
                return  # ran past the last page
            seen.update(fresh)
            records = records[: max(0, min(max_rows, MAX_ROWS) - rows)]
            rows += len(records)
            yield Page(number, result, records)
            if not result.ok or not records or not pag or rows >= min(max_rows, MAX_ROWS):
                return
            if _says_last_page(result.body):
                return
            nxt = _next_position(pag, params, result.body, len(records))
            if nxt is None:
                return
            params[pag["param"]] = nxt

    # --- helpers ---

    def _sample(self, op: Operation) -> RawRequest:
        for captured in reversed(self.samples.get(op.endpoint.key, [])):
            raw = rebuild_raw(captured, op.endpoint.required_headers)
            if raw is not None:
                return raw
        raise OperationError(
            f"{op.label} needs a fresh key or session. Run `ghost-api refresh {self.site}` first."
        )

    def _guess_pagination(self, ep: Endpoint) -> dict[str, Any] | None:
        samples = self.samples.get(ep.key, [])
        if not samples:
            return None
        last = samples[-1]
        query = dict(parse_qsl(urlsplit(last.url).query, keep_blank_values=True))
        guess = guess_pagination(query, _json_body([last]), _json(last.resp_body))
        if guess is None or (guess.location == "query" and guess.param not in query):
            return None  # only follow pages the request really has a parameter for
        return {"style": guess.style, "param": guess.param, "location": guess.location,
                "size_param": guess.size_param}  # fmt: skip

    def _first_position(self, op: Operation, pag: dict[str, Any]) -> str:
        """The first page (or offset 0): the smallest value seen, so 0-based APIs work."""
        values = []
        for req in self.samples.get(op.endpoint.key, []):
            if pag["location"] == "query":
                value = dict(parse_qsl(urlsplit(req.url).query)).get(pag["param"])
            else:
                value = (_json_body([req]) or {}).get(pag["param"])
            try:
                values.append(int(float(str(value))))
            except ValueError:
                continue
        default = 1 if pag["style"] == "page" else 0
        return str(min(values) if values else default)

    def _coerce(self, op: Operation, sample: RawRequest, params: dict[str, str]) -> dict[str, str]:
        """Friendlier inputs: 'football' or 'football,cricket' for a list body field."""
        body = _json(sample.body) if sample.body else None
        out = {}
        for name, value in params.items():
            current = body.get(name) if isinstance(body, dict) else None
            if isinstance(current, list) and not str(value).lstrip().startswith("["):
                value = json.dumps([v.strip() for v in str(value).split(",") if v.strip()])
            out[name] = str(value)
        return out


def is_callable(ep: Endpoint, allowed: bool) -> bool:
    """Ghost API calls an endpoint only if it reads data: by its URL, or because the user
    checked it (`allowed`), and only if it worked outside the browser."""
    if allowed:
        return True
    probe = RawRequest(ep.method, f"https://{ep.host}{ep.path_template}", {}, None)
    return ep.replayable is not False and is_replayable(probe)


def inputs_for(ep: Endpoint, samples: list[CapturedRequest]) -> list[Input]:
    """What a caller can set: path and query parameters, and top-level JSON body fields."""
    inputs = [Input(p.name, "path", p.type, (p.examples or [""])[0], True) for p in ep.path_params]
    inputs += [
        Input(p.name, "query", p.type, (p.examples or [""])[0], p.kind == "required")
        for p in ep.query_params
        if not p.sensitive
    ]
    body = _json_body(samples)
    for key, value in (body or {}).items():
        if is_sensitive_name(key):
            continue
        kind = "list" if isinstance(value, list) else type(value).__name__
        example = json.dumps(value) if isinstance(value, list | dict) else str(value)
        inputs.append(Input(key, "body", kind, example, False))
    return inputs


_WRAPPERS = ("data", "result", "item", "record", "payload")


def records_of(body: Any, single: bool = False) -> list[Any]:
    """The rows in a reply: its main list, or the reply itself as one row.

    `single`: the endpoint returns one thing (its path ends in an ID, like
    /venue/details/{id}), so lists inside it (reviews, photos) are not the rows."""
    if single and isinstance(body, dict):
        inner = next((body[k] for k in _WRAPPERS if isinstance(body.get(k), dict)), None)
        return [inner if inner is not None else body]
    items = find_items(body)
    if items is not None:
        return items
    return [body] if isinstance(body, dict) else []


def is_single(op: Operation) -> bool:
    """One record per reply: /books/{id}, not /books or /books/{id}/reviews."""
    return op.pagination is None and op.endpoint.path_template.rstrip("/").endswith("}")


def flatten(record: Any, prefix: str = "") -> dict[str, Any]:
    """{'venue': {'name': 'X'}, 'tags': ['a', 'b']} -> {'venue.name': 'X', 'tags': 'a; b'}."""
    if not isinstance(record, dict):
        return {prefix or "value": record}
    out: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten(value, name))
        elif isinstance(value, list):
            scalars = all(not isinstance(v, dict | list) for v in value)
            out[name] = "; ".join(str(v) for v in value) if scalars else json.dumps(value)
        else:
            out[name] = value
    return out


def _next_position(
    pag: dict[str, Any], params: dict[str, str], body: Any, count: int
) -> str | None:
    """The value to send for the next page, counted from what was just sent."""
    if pag["style"] == "cursor":
        return next_cursor(body)
    try:
        position = int(float(params[pag["param"]]))
    except (KeyError, ValueError):
        return None
    if pag["style"] == "page":
        return str(position + 1)
    return str(position + count)  # offset: skip what this page returned


# Reply fields that say whether another page exists.
_MORE_FIELDS = ("has_more", "hasMore", "has_next", "hasNext", "more", "has_next_page")


def _says_last_page(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    for obj in (body, body.get("meta"), body.get("pagination"), body.get("data")):
        if isinstance(obj, dict):
            for key in _MORE_FIELDS:
                if obj.get(key) is False:
                    return True
    return False


def _json(text: str | None) -> Any:
    try:
        return json.loads(text or "")
    except ValueError:
        return None


def _json_body(samples: list[CapturedRequest]) -> dict[str, Any] | None:
    for req in reversed(samples):
        data = _json(req.req_body)
        if isinstance(data, dict):
            return data
    return None


def load_plan(stored: str | None) -> NamingPlan | None:
    if not stored:
        return None
    try:
        return NamingPlan.model_validate_json(stored)
    except ValueError:
        return None
