"""Render a ClientSpec as a TypeScript client (FR-33).

Output, in output/<domain>/:
    <name>_client.ts         interfaces + client class (fetch-based: Node 18+ and browsers)
    <name>_client.test.ts    live tests for Node's built-in runner (`node --test`)

The same ClientSpec drives both languages, so methods, arguments, defaults, credentials
and pagination match the Python client. Differences:
  - Method and argument names are camelCase; optional arguments go in an options object.
  - Interfaces keep the API's original field names.
  - Types are compile-time only; responses are not validated at runtime like pydantic.
  - Only erasable TypeScript syntax is used, so Node 23.6+ can run the files directly.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ghost_api import __version__
from ghost_api.capture.filters import REDACTED, redact_text
from ghost_api.config import USER_AGENT_SUFFIX
from ghost_api.generator.models_gen import FieldDef
from ghost_api.generator.python_gen import (
    TEMPLATES,
    GeneratedClient,
    GenerationError,
    module_name,
)
from ghost_api.generator.spec import ClientSpec, CredentialSpec, MethodSpec, ParamSpec

PRIMITIVE_TS = {
    "str": "string",
    "int": "number",
    "float": "number",
    "bool": "boolean",
    "None": "null",
    "Any": "unknown",
}
_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


# --- Python type expression -> TypeScript type ---------------------------------------------


def ts_type(py: str) -> str:
    """'list[Quote] | None' -> 'Quote[] | null'; 'dict[str, Any]' -> 'Record<string, unknown>'."""
    parts = _split_top(py, "|")
    out = [_ts_single(p.strip()) for p in parts]
    return " | ".join(dict.fromkeys(out))


def _ts_single(py: str) -> str:
    if py in PRIMITIVE_TS:
        return PRIMITIVE_TS[py]
    if py.startswith("list[") and py.endswith("]"):
        inner = ts_type(py[5:-1])
        return f"({inner})[]" if len(_split_top(inner, "|")) > 1 else f"{inner}[]"
    if py.startswith("dict[") and py.endswith("]"):
        _key, value = _split_top(py[5:-1], ",")
        return f"Record<string, {ts_type(value.strip())}>"
    return py  # a model name


def _split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` outside brackets."""
    parts, depth, current = [], 0, ""
    for ch in text:
        depth += ch in "[(<"
        depth -= ch in "])>"
        if ch == sep and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


def js_literal(py_literal: str | None) -> str:
    """A Python literal (from the spec) as JavaScript: True -> true, ['a'] -> ["a"]."""
    if py_literal is None:
        return "undefined"
    try:
        return json.dumps(ast.literal_eval(py_literal))
    except (ValueError, SyntaxError):
        return "undefined"


def camel(name: str) -> str:
    """'hits_per_page' -> 'hitsPerPage'; trailing '_' from Python keywords is dropped."""
    head, *rest = name.rstrip("_").split("_")
    out = head + "".join(w[:1].upper() + w[1:] for w in rest if w)
    return out if _IDENT_RE.match(out) and out not in _TS_RESERVED else f"{out}Value"


_TS_RESERVED = {
    "default",
    "delete",
    "function",
    "new",
    "this",
    "var",
    "void",
    "with",
    "in",
    "instanceof",
    "typeof",
    "class",
    "enum",
    "export",
    "import",
    "super",
    "case",
    "catch",
    "const",
    "continue",
    "debugger",
    "do",
    "else",
    "finally",
    "for",
    "if",
    "return",
    "switch",
    "throw",
    "try",
    "while",
    "yield",
    "let",
    "static",
}


def ts_prop(field: FieldDef) -> str:
    """Interface property: the API's own field name, quoted when it isn't an identifier."""
    key = field.alias or field.name
    return key if _IDENT_RE.match(key) else json.dumps(key)


# --- rendering -----------------------------------------------------------------------------


def _path_expr(method: MethodSpec) -> str:
    """'/api/books/{id}' -> `/api/books/${encodeURIComponent(String(id))}`."""
    expr = method.path.replace("`", "\\`")
    expr = re.sub(
        r"\{(\w+)\}", lambda m: "${encodeURIComponent(String(" + camel(m.group(1)) + "))}", expr
    )
    return f"`{expr}`"


def _value(p: ParamSpec) -> str:
    """How a method reads an argument, applying its default."""
    ref = camel(p.name) if p.required else f"opts.{camel(p.name)}"
    if p.required or p.default is None or p.default == "None":
        return ref
    return f"{ref} ?? {js_literal(p.default)}"


def _creds(method: MethodSpec, spec: ClientSpec, location: str) -> list[CredentialSpec]:
    wanted = set(method.credentials)
    return [c for c in spec.credentials if c.arg in wanted and location in ("any", c.location)]


def render_typescript(spec: ClientSpec) -> dict[str, str]:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
        autoescape=False,  # generating TypeScript, not HTML
    )
    env.filters.update(
        ts_type=ts_type,
        ts_prop=ts_prop,
        camel=camel,
        js=js_literal,
        json=json.dumps,
        path_expr=_path_expr,
        value=_value,
        creds=_creds,
        doc=lambda t: str(t).replace("*/", "* /").strip(),
    )
    module = module_name(spec)
    context: dict[str, Any] = {
        "spec": spec,
        "module": module,
        "version": __version__,
        "generated_on": datetime.now(UTC).strftime("%Y-%m-%d"),
        "user_agent": f"{module}/{__version__} (generated by {USER_AGENT_SUFFIX})",
    }
    return {
        f"{module}.ts": env.get_template("client.ts.j2").render(context),
        f"{module}.test.ts": env.get_template("client.test.ts.j2").render(context),
    }


def check_typescript(files: dict[str, str]) -> None:
    for name, source in files.items():
        if REDACTED in source:
            raise GenerationError(f"{name} contains a redacted placeholder")
        if redact_text(source) != source:
            raise GenerationError(f"{name} contains a value that looks like a secret")


def write_typescript(spec: ClientSpec, out_dir: Path) -> GeneratedClient:
    files = render_typescript(spec)
    check_typescript(files)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        (out_dir / name).write_text(source, encoding="utf-8", newline="\n")
    module = module_name(spec)
    client = out_dir / f"{module}.ts"
    from ghost_api.generator.package import typescript_package  # it imports this module

    typescript_package(spec, module, files[f"{module}.ts"], out_dir)
    return GeneratedClient(
        directory=out_dir,
        module=module,
        client_path=client,
        models_path=client,  # interfaces live in the client file
        tests_path=out_dir / f"{module}.test.ts",
        formatted=False,
    )
