"""`ghost-api serve`: stored sites as a clean local REST API.

    GET /                                       what's served, with example URLs
    GET /{site}/{operation}?name=value&pages=2  rows as JSON (or &format=csv for Excel)
    GET /docs                                   interactive docs (OpenAPI)

A site with a section uses "~" in its URL ("turftown.in~chennai"). Listens on 127.0.0.1
only, GET only, and checks the Host header, like the dashboard.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from ghost_api import __version__
from ghost_api.export import tabular
from ghost_api.probe.replay import ReplayRefused
from ghost_api.runtime import Operation, OperationError
from ghost_api.serve import ServedSite
from ghost_api.site import to_url_id


def create_rest_app(sites: dict[str, ServedSite], allowed_hosts: set[str] | None = None) -> FastAPI:
    app = FastAPI(
        title="Ghost API: websites as APIs",
        version=__version__,
        description=(
            "Read-only endpoints discovered by Ghost API. Each call goes to the real site, "
            "one request per second, respecting robots.txt. Personal data is hidden."
        ),
    )

    @app.middleware("http")
    async def host_check(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if allowed_hosts and request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Unknown host"}, status_code=403)
        return await call_next(request)

    @app.get("/", summary="What is served here")
    def index(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return {
            "docs": f"{base}/docs",
            "sites": [
                {
                    "site": s.site,
                    "operations": [
                        {
                            "name": op.name,
                            "summary": op.summary,
                            "url": f"{base}/{to_url_id(s.site)}/{op.name}",
                            "inputs": {i.name: i.example for i in op.inputs},
                        }
                        for op in s.operations
                    ],
                }
                for s in sites.values()
            ],
        }

    for served in sites.values():
        for op in served.operations:
            app.add_api_route(
                f"/{to_url_id(served.site)}/{op.name}",
                _handler(served, op),
                methods=["GET"],
                summary=op.summary,
                description=f"Calls `{op.label}` on {served.site}. Inputs are optional; the "
                "examples are what the site itself sent.",
                tags=[served.site],
            )
    return app


def _handler(served: ServedSite, op: Operation) -> Callable[..., Response]:
    """A route with one query parameter per input, so the docs show them all."""
    aliases: dict[str, str] = {}
    params = [
        inspect.Parameter(
            "pages", inspect.Parameter.KEYWORD_ONLY, annotation=int,
            default=Query(1, ge=1, le=20, description="How many pages to follow."),
        ),
        inspect.Parameter(
            "max_rows", inspect.Parameter.KEYWORD_ONLY, annotation=int,
            default=Query(1000, ge=1, le=10_000, description="Stop after this many rows."),
        ),
        inspect.Parameter(
            "format", inspect.Parameter.KEYWORD_ONLY, annotation=str,
            default=Query("json", pattern="^(json|csv)$", description="json or csv"),
        ),
    ]  # fmt: skip
    for i, item in enumerate(op.inputs):
        name = f"input_{i}"
        aliases[name] = item.name
        description = f"{item.location} · {item.type}"
        if item.example:
            description += f" · e.g. {item.example}"
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=str | None,
                default=Query(None, alias=item.name, description=description),
            )
        )

    def handler(**kwargs: Any) -> Response:
        inputs = {aliases[k]: v for k, v in kwargs.items() if k in aliases and v is not None}
        try:
            result = served.fetch(op, inputs, kwargs["pages"], kwargs["max_rows"])
        except (OperationError, ReplayRefused) as exc:
            raise HTTPException(409, str(exc)) from exc
        if result["error"] and not result["rows"]:
            raise HTTPException(502, f"The site answered: {result['error']}")
        if kwargs["format"] == "csv":
            return Response(
                tabular.to_bytes(result["data"], "csv"),
                media_type=tabular.MEDIA_TYPES["csv"],
                headers={"X-Ghost-Rows": str(result["rows"])},
            )
        return JSONResponse(result)

    handler.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    handler.__name__ = f"{op.name}_{abs(hash(served.site)) % 10_000}"
    return handler
