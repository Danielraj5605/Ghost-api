"""`ghost-api verify-spec`: compare a published OpenAPI spec with what the site really does.

Documentation drifts from reality. Using the endpoints Ghost API observed and verified for
a site, this reports:

    undocumented     the site uses it, the spec doesn't mention it
    not seen         the spec documents it, the site never used it while explored
                     (it may be unused, removed, or just not reached)
    field gaps       for endpoints in both: reply fields the spec doesn't list, and spec
                     fields the real replies never contained
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ghost_api.analysis.graphql import split_template
from ghost_api.analysis.models import Endpoint

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


class SpecError(Exception):
    """The file is not an OpenAPI document Ghost API can read."""


@dataclass
class EndpointCheck:
    endpoint: str  # "GET /api/items/{}"
    status: str  # "documented", "undocumented", "not seen"
    extra_fields: list[str] = field(default_factory=list)  # in replies, not in the spec
    missing_fields: list[str] = field(default_factory=list)  # in the spec, never in replies


def load_spec(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise SpecError(f"{path.name} can't be read: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        raise SpecError(f"{path.name} is not an OpenAPI document (no 'paths')")
    return data


def _key(method: str, path: str) -> str:
    return f"{method.upper()} {re.sub(r'{[^}]*}', '{}', path.rstrip('/') or '/')}"


def _resolve(spec: dict[str, Any], node: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(node, dict) or depth > 10:
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target: Any = spec
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return _resolve(spec, target, depth + 1)
    return node


def _spec_fields(spec: dict[str, Any], operation: dict[str, Any]) -> set[str] | None:
    """Top-level fields of the operation's success reply, or None when not described."""
    for code in ("200", "201", "2XX", "default"):
        reply = _resolve(spec, (operation.get("responses") or {}).get(code))
        content = reply.get("content") or {}
        media = next((v for k, v in content.items() if "json" in k), None)
        if media is None:
            continue
        schema = _resolve(spec, media.get("schema"))
        if schema.get("type") == "array" or "items" in schema:
            schema = _resolve(spec, schema.get("items"))
        props = schema.get("properties")
        if isinstance(props, dict):
            return set(props)
    return None


def _seen_fields(ep: Endpoint) -> set[str]:
    schema = ep.response_schema or {}
    if schema.get("type") == "array" or "items" in schema:
        schema = schema.get("items") or {}
    props = schema.get("properties")
    return set(props) if isinstance(props, dict) else set()


def compare(spec: dict[str, Any], endpoints: list[Endpoint]) -> list[EndpointCheck]:
    documented: dict[str, dict[str, Any]] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method in METHODS and isinstance(operation, dict):
                documented[_key(method, str(path))] = operation
    observed: dict[str, Endpoint] = {}
    for ep in endpoints:
        path, _ = split_template(ep.path_template)
        observed.setdefault(_key(ep.method, path), ep)
    checks = []
    for key in sorted(observed.keys() | documented.keys()):
        if key not in documented:
            checks.append(EndpointCheck(key, "undocumented"))
        elif key not in observed:
            checks.append(EndpointCheck(key, "not seen"))
        else:
            check = EndpointCheck(key, "documented")
            listed, seen = _spec_fields(spec, documented[key]), _seen_fields(observed[key])
            if listed is not None and seen:
                check.extra_fields = sorted(seen - listed)
                check.missing_fields = sorted(listed - seen)
            checks.append(check)
    return checks


def to_json(checks: list[EndpointCheck]) -> list[dict[str, Any]]:
    return [asdict(c) for c in checks]
