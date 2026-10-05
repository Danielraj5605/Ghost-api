"""Infer JSON schemas from sample bodies (FR-19).

genson merges samples: a field missing from any sample is left out of "required"
(so it becomes optional), and a field seen as both null and a string becomes
["null", "string"].
"""

from __future__ import annotations

import json
from typing import Any

from genson import SchemaBuilder


def parse_json(body: str | None) -> Any | None:
    """Parse a body, returning None if it isn't valid JSON."""
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def build_schema(samples: list[Any]) -> dict[str, Any] | None:
    if not samples:
        return None
    builder = SchemaBuilder(schema_uri=None)
    for sample in samples:
        builder.add_object(sample)
    schema: dict[str, Any] = builder.to_schema()
    return schema


def type_label(schema: dict[str, Any] | None) -> str:
    """Short human-readable description: 'object', 'array[object]', 'string|null'."""
    if not schema:
        return "?"
    if "anyOf" in schema:
        return "|".join(type_label(s) for s in schema["anyOf"])
    kind = schema.get("type", "any")
    if isinstance(kind, list):
        return "|".join(kind)
    if kind == "array":
        return f"array[{type_label(schema.get('items'))}]"
    return str(kind)


def top_level_fields(schema: dict[str, Any] | None, limit: int = 8) -> str:
    """'has_next, page, quotes[] (+2 more)' — a one-line summary for tables."""
    if not schema:
        return "-"
    target = schema
    prefix = ""
    if schema.get("type") == "array":
        target = schema.get("items") or {}
        prefix = "[] of "
    props = target.get("properties")
    if not props:
        return prefix + type_label(target)
    names = [f"{k}[]" if v.get("type") == "array" else k for k, v in props.items()]
    shown = ", ".join(names[:limit])
    more = f" (+{len(names) - limit} more)" if len(names) > limit else ""
    return prefix + shown + more


def field_paths(schema: dict[str, Any] | None, max_depth: int = 3, limit: int = 20) -> list[str]:
    """Leaf field paths, e.g. ['page', 'quotes[].author.name', 'quotes[].tags[]'].

    Lets the agent see what data an endpoint already returns, including nested fields.
    """
    paths: list[str] = []

    def walk(node: dict[str, Any], prefix: str, depth: int) -> None:
        if len(paths) >= limit:
            return
        kind = node.get("type")
        if kind == "array" or (isinstance(kind, list) and "array" in kind):
            items = node.get("items") or {}
            if items.get("properties") and depth < max_depth:
                walk(items, prefix + "[]", depth)
            else:
                paths.append(prefix + "[]")
            return
        props = node.get("properties")
        if props and depth < max_depth:
            for key, child in props.items():
                walk(child, f"{prefix}.{key}" if prefix else key, depth + 1)
                if len(paths) >= limit:
                    return
        elif prefix:
            paths.append(prefix)

    if schema:
        walk(schema, "", 0)
    return paths
