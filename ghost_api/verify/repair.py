"""Targeted schema fixes for fields that failed validation."""

from __future__ import annotations

import copy
from typing import Any


def relax_schema(
    schema: dict[str, Any] | None, locations: list[tuple[str, ...]]
) -> dict[str, Any] | None:
    """Make each failing field accept anything (and be optional).

    `locations` are pydantic error paths such as ('quotes', '0', 'author', 'name');
    numeric parts are list indexes and are skipped while walking the schema.
    """
    if schema is None:
        return None
    out = copy.deepcopy(schema)
    for loc in locations:
        _relax(out, [p for p in loc if not p.isdigit()])
    return out


def _relax(node: dict[str, Any], path: list[str]) -> None:
    if not path:
        return
    while node.get("type") == "array" and isinstance(node.get("items"), dict):
        node = node["items"]
    props = node.get("properties")
    if not isinstance(props, dict) or path[0] not in props:
        return
    key = path[0]
    if len(path) == 1:
        props[key] = {}  # any value
        required = node.get("required")
        if isinstance(required, list) and key in required:
            required.remove(key)
        return
    _relax(props[key], path[1:])
