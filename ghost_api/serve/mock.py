"""`ghost-api mock SITE`: a local copy of a site's API, from replies captured while exploring.

Front-end developers and tests can work without the real site, offline, and without
sending it any traffic. Every endpoint answers with a real reply seen earlier (personal
data already hidden), at the same path:

    GET http://127.0.0.1:8901/api/quotes?page=2   ->  the reply captured for page=2,
                                                      or the closest one for that endpoint

Matching: the endpoint is found by method and path template; among its captured replies,
the one whose query matches the request best wins. `X-Ghost-Mock: exact|closest` says which.
Listens on 127.0.0.1 only and answers CORS preflights, so a page on localhost can call it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from ghost_api import __version__
from ghost_api.analysis.analyzer import endpoint_keys
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.models import CapturedRequest
from ghost_api.memory.store import Store

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


@dataclass
class MockRoute:
    endpoint: Endpoint
    pattern: re.Pattern[str]
    samples: list[CapturedRequest]

    def best(self, path: str, query: dict[str, str]) -> tuple[CapturedRequest, bool]:
        """The captured reply closest to the request (same path first, then the most
        matching query values), and whether it matches exactly."""

        def parts(sample: CapturedRequest) -> tuple[str, dict[str, str]]:
            url = urlsplit(sample.url)
            return url.path.rstrip("/"), dict(parse_qsl(url.query, keep_blank_values=True))

        def score(sample: CapturedRequest) -> tuple[bool, int, int]:
            seen_path, seen = parts(sample)
            same = sum(1 for k, v in query.items() if seen.get(k) == v)
            return seen_path == path.rstrip("/"), same, -abs(len(seen) - len(query))

        chosen = max(self.samples, key=score)
        return chosen, parts(chosen) == (path.rstrip("/"), query)


def template_pattern(template: str) -> re.Pattern[str]:
    parts = re.split(r"\{[^}]*\}", template)
    return re.compile("[^/]+".join(re.escape(p) for p in parts) + r"/?\Z")


def build_routes(store: Store, site: str) -> list[MockRoute]:
    endpoints = store.current_endpoints(site)
    requests = store.requests_for_domain(site)
    samples: dict[str, list[CapturedRequest]] = {}
    for key, req in zip(endpoint_keys(requests), requests, strict=False):
        if req.resp_body and 200 <= req.status < 300 and not req.truncated:
            samples.setdefault(key, []).append(req)
    routes = [
        MockRoute(ep, template_pattern(ep.path_template), samples[ep.key])
        for ep in endpoints
        if ep.key in samples
    ]
    # Literal paths before templated ones: /api/items/featured before /api/items/{id}.
    return sorted(routes, key=lambda r: r.endpoint.path_template.count("{"))


def create_mock_app(
    routes: list[MockRoute], site: str, allowed_hosts: set[str] | None = None
) -> FastAPI:
    app = FastAPI(title=f"Mock of {site}", version=__version__, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def host_check(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if allowed_hosts and request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Unknown host"}, status_code=403)
        return await call_next(request)

    @app.get("/__ghost/routes")
    def listing() -> dict[str, Any]:
        return {
            "site": site,
            "routes": [
                {"method": r.endpoint.method, "path": r.endpoint.path_template,
                 "replies": len(r.samples)}
                for r in routes
            ],
        }  # fmt: skip

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    )
    async def answer(path: str, request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=CORS)
        method = "GET" if request.method == "HEAD" else request.method
        url_path = "/" + path
        query = dict(request.query_params)
        for route in routes:
            if route.endpoint.method == method and route.pattern.match(url_path):
                sample, exact = route.best(url_path, query)
                return Response(
                    content=sample.resp_body or "",
                    status_code=sample.status,
                    media_type=sample.resp_content_type or "application/json",
                    headers={**CORS, "X-Ghost-Mock": "exact" if exact else "closest"},
                )
        known = [f"{r.endpoint.method} {r.endpoint.path_template}" for r in routes]
        body = {"detail": f"No captured reply for {method} {url_path}", "known": known}
        return Response(json.dumps(body), 404, media_type="application/json", headers=CORS)

    return app
