"""Turn inferred JSON schemas into pydantic model definitions (FR-28).

- Nested objects become their own classes, named after the field (quotes[] -> Quote).
- Objects with the same structure share one class.
- Fields missing from some samples, or seen as null, become `T | None = None`.
- Field names that aren't valid Python get a snake_case name plus an alias.
- Generated models allow extra fields, so a new field in the API doesn't break them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ghost_api.analysis.url_grouping import singular
from ghost_api.generator.naming import pascal, snake, unique

PRIMITIVES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "null": "None",
}
# Names that would shadow pydantic BaseModel attributes (json, copy, schema, model_*...).
_RESERVED = {n for n in dir(BaseModel) if not n.startswith("_")}


@dataclass
class FieldDef:
    name: str  # Python attribute name
    alias: str | None  # original JSON key, when different
    type: str  # type expression, e.g. "list[Quote] | None"
    optional: bool


@dataclass
class ModelDef:
    name: str
    fields: list[FieldDef] = field(default_factory=list)
    doc: str = ""


class ModelBuilder:
    def __init__(self, reserved_names: set[str] | None = None) -> None:
        self.models: list[ModelDef] = []
        self._by_signature: dict[tuple[Any, ...], str] = {}
        self._used: set[str] = set(reserved_names or ())

    def root_type(self, schema: dict[str, Any] | None, name: str, doc: str = "") -> str:
        """Type expression for a whole response; creates models as needed."""
        if not schema:
            return "Any"
        if _is_object(schema):
            return self._model(schema, name, doc, exact_name=True)
        return self.type_for(schema, name)

    def type_for(self, schema: dict[str, Any] | None, hint: str) -> str:
        if not schema:
            return "Any"
        if "anyOf" in schema:
            return _union(self.type_for(s, hint) for s in schema["anyOf"])
        kinds = schema.get("type")
        if isinstance(kinds, list):
            parts = []
            for k in kinds:
                sub = {**schema, "type": k}
                parts.append(self.type_for(sub, hint))
            return _union(parts)
        if kinds == "object":
            if schema.get("properties"):
                return self._model(schema, hint)
            return "dict[str, Any]"
        if kinds == "array":
            items = schema.get("items")
            item_hint = pascal(singular(snake(hint))) if hint else "Item"
            return f"list[{self.type_for(items, item_hint) if items else 'Any'}]"
        return PRIMITIVES.get(str(kinds), "Any")

    def _model(
        self, schema: dict[str, Any], hint: str, doc: str = "", exact_name: bool = False
    ) -> str:
        props: dict[str, Any] = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        fields: list[FieldDef] = []
        used_fields: set[str] = set()
        for key, sub in props.items():
            type_expr = self.type_for(sub, pascal(key))
            if type_expr == "None":  # only ever seen as null: real type unknown
                type_expr = "Any"
            nullable = "None" in _top_level_members(type_expr) or type_expr == "Any"
            optional = key not in required or nullable
            if optional and not nullable:
                type_expr = _union([type_expr, "None"])
            py = snake(key)
            if py in _RESERVED or py.startswith("model_"):
                py += "_"
            py = unique(py, used_fields)
            fields.append(FieldDef(py, None if py == key else key, type_expr, optional))

        signature = tuple((f.alias or f.name, f.type, f.optional) for f in fields)
        if signature in self._by_signature and not exact_name:
            return self._by_signature[signature]
        name = unique(pascal(hint) or "Model", self._used)
        self.models.append(ModelDef(name, fields, doc))
        self._by_signature.setdefault(signature, name)
        return name


def _is_object(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "object" and bool(schema.get("properties"))


def _top_level_members(type_expr: str) -> list[str]:
    """'int | list[int | None] | None' -> ['int', 'list[int | None]', 'None']: split
    only outside brackets, or nested unions fall apart (seen on Google Maps data)."""
    members, depth, start = [], 0, 0
    for i, ch in enumerate(type_expr):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif depth == 0 and type_expr.startswith(" | ", i):
            members.append(type_expr[start:i])
            start = i + 3
    members.append(type_expr[start:])
    return members


def _union(parts: Any) -> str:
    seen: list[str] = []
    for part in parts:
        for p in _top_level_members(str(part)):
            if p not in seen:
                seen.append(p)
    if "Any" in seen:
        return "Any"
    seen.sort(key=lambda p: p == "None")  # None last
    return " | ".join(seen) or "Any"
