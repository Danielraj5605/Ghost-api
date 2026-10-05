"""Export the endpoint map as an OpenAPI 3.1 document (FR-30).

3.1 is used because the inferred schemas are plain JSON Schema (for example
`"type": ["null", "string"]`), which 3.1 supports and 3.0 does not.

- Only read-only endpoints are exported, matching the generated client.
- Secrets never appear: secret parameters and headers become security schemes.
- Endpoints on other hosts (third-party APIs) get their own `servers` entry.
- Each operation carries `x-ghost-api` with status, confidence and sample count.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import yaml

from ghost_api import __version__
from ghost_api.analysis.models import Endpoint
from ghost_api.capture.filters import is_sensitive_name
from ghost_api.capture.models import RawRequest
from ghost_api.export.confidence import confidence
from ghost_api.generator.naming import EndpointNames, pascal, snake
from ghost_api.probe.replay import is_replayable

OPENAPI_VERSION = "3.1.0"
JSON_TYPES = {"integer", "number", "boolean", "string"}


def build_openapi(
    domain: str,
    start_url: str,
    endpoints: list[Endpoint],
    names: dict[str, EndpointNames],
) -> dict[str, Any]:
    parts = urlsplit(start_url)
    main_host = (parts.hostname or "").lower()
    doc: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": f"{domain} (unofficial)",
            "version": datetime.now(UTC).strftime("%Y.%m.%d"),
            "description": (
                f"The internal API of {domain}, discovered by Ghost API {__version__} from the "
                "site's own traffic. Unofficial: it can change without notice, and the site's "
                "terms of service apply."
            ),
        },
        "servers": [{"url": f"{parts.scheme}://{parts.netloc}"}],
        "paths": {},
        "components": {"schemas": {}, "securitySchemes": {}},
    }
    schemas: dict[str, Any] = doc["components"]["schemas"]
    security_schemes: dict[str, Any] = doc["components"]["securitySchemes"]

    for ep in endpoints:
        if not _exportable(ep):
            continue
        n = names.get(ep.key)
        operation: dict[str, Any] = {
            "operationId": n.method_name if n else snake(f"{ep.method}_{ep.path_template}"),
            "summary": n.summary if n else f"{ep.method} {ep.path_template}",
            "parameters": [],
            "responses": {},
            "x-ghost-api": {
                "status": ep.status,
                "confidence": confidence(ep),
                "samples": ep.sample_count,
                "replayable": ep.replayable,
                **({"pagination": ep.pagination} if ep.pagination else {}),
                **({"note": ep.status_note} if ep.status_note else {}),
            },
        }
        if ep.host != main_host:
            operation["servers"] = [{"url": f"https://{ep.host}"}]

        security: dict[str, list[str]] = {}
        for pp in ep.path_params:
            operation["parameters"].append(
                {
                    "name": pp.name,
                    "in": "path",
                    "required": True,
                    "schema": _param_schema(pp.type, pp.examples),
                }
            )
        for qp in ep.query_params:
            if qp.sensitive or is_sensitive_name(qp.name):
                scheme = _scheme_name(qp.name)
                security_schemes[scheme] = {"type": "apiKey", "in": "query", "name": qp.name}
                security[scheme] = []
                continue
            schema = _param_schema(qp.type, qp.examples)
            is_page = bool(ep.pagination) and ep.pagination.get("param") == qp.name  # type: ignore[union-attr]
            if qp.constant and qp.examples:
                schema["default"] = _typed(qp.examples[0], qp.type)
            elif is_page and qp.examples:
                # Paging starts at the lowest value seen (page 1, offset 0...).
                values = [_typed(e, qp.type) for e in qp.examples]
                numbers = [v for v in values if isinstance(v, int | float)]
                schema["default"] = min(numbers) if numbers else values[0]
            operation["parameters"].append(
                {
                    "name": qp.name,
                    "in": "query",
                    "required": qp.required and not qp.constant and not is_page,
                    "schema": schema,
                }
            )
        for header in ep.required_headers:
            if header.lower() == "cookie":
                security_schemes["sessionCookie"] = {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "session",
                    "description": "Session cookie(s) from a logged-in browser.",
                }
                security["sessionCookie"] = []
            elif header.lower() == "authorization":
                kind = "basic" if ep.auth_type == "basic" else "bearer"
                security_schemes[f"{kind}Auth"] = {"type": "http", "scheme": kind}
                security[f"{kind}Auth"] = []
            elif is_sensitive_name(header):
                scheme = _scheme_name(header)
                security_schemes[scheme] = {"type": "apiKey", "in": "header", "name": header}
                security[scheme] = []
            else:
                operation["parameters"].append(
                    {"name": header, "in": "header", "required": True, "schema": {"type": "string"}}
                )
        if security:
            operation["security"] = [security]
        if not operation["parameters"]:
            del operation["parameters"]

        if ep.request_schema is not None:
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": _clean(ep.request_schema)}},
            }
        model = n.response_model if n else pascal(f"{ep.method}_{ep.path_template}") + "Response"
        if ep.response_schema is not None:
            schemas[model] = _clean(ep.response_schema)
            content = {"application/json": {"schema": {"$ref": f"#/components/schemas/{model}"}}}
            operation["responses"]["200"] = {"description": "Success", "content": content}
        else:
            operation["responses"]["200"] = {"description": "Success (no JSON sample captured)"}

        path_item = doc["paths"].setdefault(ep.path_template, {})
        path_item[ep.method.lower()] = operation

    if not schemas:
        del doc["components"]["schemas"]
    if not security_schemes:
        del doc["components"]["securitySchemes"]
    if not doc["components"]:
        del doc["components"]
    return doc


def to_yaml(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


def _exportable(ep: Endpoint) -> bool:
    probe = RawRequest(ep.method, f"https://{ep.host}{ep.path_template}", {}, None)
    return is_replayable(probe) and ep.replayable is not False


def _param_schema(json_type: str, examples: list[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": json_type if json_type in JSON_TYPES else "string"}
    if examples:
        schema["examples"] = [_typed(e, json_type) for e in examples[:3]]
    return schema


def _typed(value: str, json_type: str) -> Any:
    try:
        if json_type == "integer":
            return int(value)
        if json_type == "number":
            return float(value)
    except ValueError:
        return value
    if json_type == "boolean":
        return value.lower() == "true"
    return value


def _scheme_name(wire: str) -> str:
    return pascal(wire)[:1].lower() + pascal(wire)[1:]


def _clean(schema: dict[str, Any]) -> dict[str, Any]:
    """A copy without genson's '$schema' marker (not allowed inside OpenAPI)."""
    out = copy.deepcopy(schema)
    out.pop("$schema", None)
    return out
