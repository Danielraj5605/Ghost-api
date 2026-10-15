"""Turn captured traffic into an endpoint map. Deterministic: no LLM involved.

Every endpoint comes from real captured requests (NFR-12).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ghost_api.analysis.graphql import operation_of
from ghost_api.analysis.models import Endpoint, PathParam, QueryParam
from ghost_api.analysis.schema import build_schema, parse_json
from ghost_api.analysis.url_grouping import group_urls, infer_type
from ghost_api.capture.filters import REDACTED
from ghost_api.capture.models import CapturedRequest

MAX_EXAMPLES = 5


def analyze(requests: list[CapturedRequest]) -> list[Endpoint]:
    if not requests:
        return []
    matches = group_urls([(r.method, r.url) for r in requests])
    templates = _templates(requests, matches)

    grouped: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for i, m in enumerate(matches):
        grouped[(m.method, m.host, templates[i])].append(i)

    endpoints: list[Endpoint] = []
    for (method, host, template), idxs in grouped.items():
        reqs = [requests[i] for i in idxs]
        names = matches[idxs[0]].param_names
        path_params = [
            _path_param(name, [matches[i].param_values[k] for i in idxs])
            for k, name in enumerate(names)
        ]
        request_samples = _bodies(reqs, request=True)
        response_samples = _bodies(reqs, request=False)
        endpoints.append(
            Endpoint(
                method=method,
                host=host,
                path_template=template,
                path_params=path_params,
                query_params=classify_query_params([r.url for r in reqs]),
                request_schema=build_schema(request_samples),
                response_schema=build_schema(response_samples),
                sample_count=len(reqs),
                request_samples=len(request_samples),
                response_samples=len(response_samples),
                statuses=sorted({r.status for r in reqs}),
                example_urls=_unique([r.url for r in reqs])[:MAX_EXAMPLES],
                third_party=any(r.third_party for r in reqs),
            )
        )
    endpoints.sort(key=lambda e: (e.host, e.path_template, e.method))
    return endpoints


def endpoint_keys(requests: list[CapturedRequest]) -> list[str]:
    """The Endpoint.key each request belongs to (same grouping as analyze())."""
    matches = group_urls([(r.method, r.url) for r in requests])
    templates = _templates(requests, matches)
    return [f"{m.method} {m.host}{t}" for m, t in zip(matches, templates, strict=True)]


def _templates(requests: list[CapturedRequest], matches: list[Any]) -> list[str]:
    """Each request's template; a named GraphQL operation gets its own: /graphql#GetUser."""
    out = []
    for req, m in zip(requests, matches, strict=True):
        op = operation_of(req.method, req.url, req.req_body)
        out.append(f"{m.template}#{op}" if op else m.template)
    return out


def classify_query_params(urls: list[str]) -> list[QueryParam]:
    """Classify each query parameter as required, optional or constant (FR-18)."""
    total = len(urls)
    seen: dict[str, list[str]] = defaultdict(list)
    for url in urls:
        # A parameter repeated in one URL (?tag=a&tag=b) still counts once for presence.
        per_url: dict[str, str] = {}
        for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True):
            per_url.setdefault(k, v)
        for k, v in per_url.items():
            seen[k].append(v)

    params: list[QueryParam] = []
    for name, values in seen.items():
        sensitive = any(v == REDACTED for v in values)
        real = [v for v in values if v != REDACTED]
        distinct = _unique(real)
        params.append(
            QueryParam(
                name=name,
                required=len(values) == total,
                constant=total > 1 and len(values) == total and len(set(values)) == 1,
                type=infer_type(distinct) if distinct else "string",
                examples=distinct[:MAX_EXAMPLES],
                sensitive=sensitive,
            )
        )
    return params


def _path_param(name: str, values: list[str]) -> PathParam:
    distinct = _unique(values)
    return PathParam(name=name, type=infer_type(distinct), examples=distinct[:MAX_EXAMPLES])


def _bodies(reqs: list[CapturedRequest], request: bool) -> list[Any]:
    """Parsed JSON bodies. Responses only count when successful and not truncated."""
    out: list[Any] = []
    for r in reqs:
        if request:
            body = parse_json(r.req_body)
        elif 200 <= r.status < 300 and not r.truncated:
            body = parse_json(r.resp_body)
        else:
            continue
        if body is not None:
            out.append(body)
    return out


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
