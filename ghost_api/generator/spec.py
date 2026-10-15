"""Everything the templates need to render a client, derived from the endpoint map.

Rules:
  - Only read-only endpoints become methods (GET, and search/query POSTs), the same
    rule the replayer uses. Endpoints that could not be replayed are left out.
  - Path parameters and changing required parameters are required arguments.
  - Parameters that never changed get their observed value as a default.
  - The pagination parameter is optional and starts at the observed value.
  - Secret-looking parameters/headers become credentials: constructor arguments that
    default to an environment variable. Their values are never written (NFR-16).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from ghost_api.analysis.analyzer import endpoint_keys
from ghost_api.analysis.models import Endpoint
from ghost_api.analysis.url_grouping import looks_like_id
from ghost_api.capture.filters import REDACTED, is_sensitive_name
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.generator.models_gen import ModelBuilder, ModelDef
from ghost_api.generator.naming import EndpointNames, snake, unique
from ghost_api.probe.replay import is_replayable

PY_TYPES = {"integer": "int", "number": "float", "boolean": "bool", "string": "str"}
# Headers the client sets itself, or that only browsers send.
SKIP_HEADERS = {
    "content-type",
    "content-length",
    "user-agent",
    "accept-encoding",
    "host",
    "connection",
    "cookie",
}


# Fields are generated as always present only with enough evidence; otherwise every field
# may be missing. Found on Turf Town: a field present in all 24 venues of one area (over 4
# pages) was missing for a venue in another area. Evidence is replies to at least 2
# different inputs (paging aside) across MIN_REPLIES_FOR_REQUIRED replies, or simply
# MANY_REPLIES replies.
MIN_REPLIES_FOR_REQUIRED = 3
MANY_REPLIES = 6
_PAGING_KEYS = {
    "page", "offset", "skip", "limit", "cursor", "start", "per_page", "perpage", "page_size",
    "pagesize", "size", "after", "before", "from", "hitsperpage", "hits_per_page",
}  # fmt: skip


def trust_required(reqs: list[CapturedRequest]) -> bool:
    """Is "seen in every sample" good evidence for "always present" for this endpoint?"""
    replies = {r.resp_body for r in reqs}
    if len(replies) >= MANY_REPLIES:
        return True
    inputs = {_input_signature(r) for r in reqs}
    return len(replies) >= MIN_REPLIES_FOR_REQUIRED and len(inputs) >= 2


def _input_signature(req: CapturedRequest) -> str:
    query = {
        k: v
        for k, v in parse_qsl(urlsplit(req.url).query, keep_blank_values=True)
        if k.lower() not in _PAGING_KEYS
    }
    body = _json_dict(req.req_body) or {}
    body = {k: v for k, v in body.items() if k.lower() not in _PAGING_KEYS}
    return json.dumps([sorted(query.items()), body], sort_keys=True, default=str)


@dataclass
class ParamSpec:
    name: str  # Python argument name
    wire: str  # name on the wire
    location: Literal["path", "query", "body"]
    type: str  # Python type, e.g. "int"
    required: bool
    default: str | None = None  # Python literal, when there is one
    example: str | None = None  # Python literal used by generated tests

    @property
    def mutable_default(self) -> bool:
        return bool(self.default) and self.default[:1] in ("[", "{")  # type: ignore[index]

    @property
    def annotation(self) -> str:
        if self.required or (self.default not in (None, "None") and not self.mutable_default):
            return self.type
        return f"{self.type} | None"

    @property
    def default_literal(self) -> str:
        if self.default is None or self.mutable_default:
            return "None"
        return self.default

    @property
    def value_expr(self) -> str:
        """The value to send: list/dict defaults are filled in at call time."""
        if self.mutable_default:
            return f"{self.name} if {self.name} is not None else {self.default}"
        return self.name


@dataclass
class CredentialSpec:
    arg: str  # constructor argument
    wire: str  # header or query parameter name
    location: Literal["header", "query"]
    env: str  # environment variable that supplies it


@dataclass
class MethodSpec:
    name: str
    endpoint_key: str  # Endpoint.key this method calls
    summary: str
    http_method: str
    host: str
    path: str  # template with {python_name} placeholders
    params: list[ParamSpec]
    response_type: str
    headers: dict[str, str]
    credentials: list[str]  # CredentialSpec.arg values this method needs
    pagination: dict[str, Any] | None = None
    page_param: ParamSpec | None = None
    replayable: bool | None = None

    @property
    def required(self) -> list[ParamSpec]:
        return [p for p in self.params if p.required]

    @property
    def optional(self) -> list[ParamSpec]:
        return [p for p in self.params if not p.required]

    def by_location(self, location: str) -> list[ParamSpec]:
        return [p for p in self.params if p.location == location]

    @property
    def testable(self) -> bool:
        return self.replayable is not False and all(p.example is not None for p in self.required)


@dataclass
class ClientSpec:
    class_name: str
    domain: str
    base_url: str
    methods: list[MethodSpec] = field(default_factory=list)
    models: list[ModelDef] = field(default_factory=list)
    credentials: list[CredentialSpec] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # endpoint labels left out, with why

    @property
    def error_class(self) -> str:
        return self.class_name.removesuffix("Client") + "Error"

    @property
    def error_classes(self) -> dict[str, str]:
        """Typed errors, each a subclass of `error_class`: kind -> class name."""
        stem = self.class_name.removesuffix("Client")
        return {
            "auth": f"{stem}AuthError",  # 401, 403
            "not_found": f"{stem}NotFoundError",  # 404
            "rate_limit": f"{stem}RateLimitError",  # 429
            "server": f"{stem}ServerError",  # 5xx
        }

    @property
    def async_class(self) -> str:
        return f"Async{self.class_name}"

    @property
    def reserved_names(self) -> set[str]:
        return {self.class_name, self.async_class, self.error_class, *self.error_classes.values()}

    @property
    def model_names(self) -> list[str]:
        return [m.name for m in self.models]

    @property
    def client_imports(self) -> list[str]:
        """Models the client module refers to directly (response types)."""
        used = " ".join(m.response_type for m in self.methods)
        return [n for n in self.model_names if re.search(rf"\b{n}\b", used)]


def build_spec(
    domain: str,
    start_url: str,
    endpoints: list[Endpoint],
    requests: list[CapturedRequest],
    class_name: str,
    names: dict[str, EndpointNames],
    allow: set[str] | None = None,
) -> ClientSpec:
    """`allow`: endpoints the user has checked only read data, by label
    ("POST /api/search" or "POST api.example.com/search"). Ghost API never decides
    that by itself for a POST without a read word in its URL."""
    parts = urlsplit(start_url)
    spec = ClientSpec(class_name, domain, f"{parts.scheme}://{parts.netloc}")
    main_host = (parts.hostname or "").lower()
    keys = endpoint_keys(requests)
    samples: dict[str, list[CapturedRequest]] = defaultdict(list)
    for key, req in zip(keys, requests, strict=False):
        samples[key].append(req)

    builder = ModelBuilder(reserved_names=spec.reserved_names)
    env_prefix = snake(class_name.removesuffix("Client")).upper()
    creds: dict[tuple[str, str], CredentialSpec] = {}

    for ep in endpoints:
        reqs = samples.get(ep.key, [])
        reason = _skip_reason(ep, reqs, allowed=is_allowed(ep, allow or set()))
        if reason:
            spec.skipped.append(f"{ep.method} {ep.host}{ep.path_template}: {reason}")
            continue
        n = names[ep.key]
        used: set[str] = {"self"}
        params: list[ParamSpec] = []
        path = ep.path_template
        for pp in ep.path_params:
            arg = unique(snake(pp.name), used)
            path = path.replace("{" + pp.name + "}", "{" + arg + "}")
            params.append(
                ParamSpec(
                    arg,
                    pp.name,
                    "path",
                    PY_TYPES.get(pp.type, "str"),
                    True,
                    example=_literal(pp.examples[0], pp.type) if pp.examples else None,
                )
            )

        method_creds: list[str] = []
        page = ep.pagination or {}
        for qp in ep.query_params:
            if qp.sensitive or is_sensitive_name(qp.name):
                method_creds.append(_credential(creds, qp.name, "query", env_prefix))
                continue
            arg = unique(snake(qp.name), used)
            first = qp.examples[0] if qp.examples else None
            literal = _literal(first, qp.type) if first is not None else None
            is_page = page.get("location") == "query" and page.get("param") == qp.name
            if qp.constant or is_page:
                # A cursor starts empty (first page); page/offset start where we saw them.
                start = None if is_page and page.get("style") == "cursor" else literal
                params.append(
                    ParamSpec(
                        arg,
                        qp.name,
                        "query",
                        PY_TYPES.get(qp.type, "str"),
                        False,
                        default=start,
                        example=start,
                    )
                )
            else:
                required = qp.required and is_user_input(qp.name, qp.examples)
                params.append(
                    ParamSpec(
                        arg,
                        qp.name,
                        "query",
                        PY_TYPES.get(qp.type, "str"),
                        required,
                        # Optional settings default to a value seen working.
                        default=None if required or not qp.required else literal,
                        example=literal,
                    )
                )

        params += _body_params(reqs, used, page)

        headers: dict[str, str] = {}
        sample_headers = reqs[-1].req_headers if reqs else {}
        for h in ep.required_headers:
            if h.lower() == "cookie" or is_sensitive_name(h):
                method_creds.append(_credential(creds, h, "header", env_prefix))
            elif h.lower() not in SKIP_HEADERS:
                value = next((v for k, v in sample_headers.items() if k.lower() == h.lower()), "")
                if value and value != REDACTED:
                    headers[h] = value

        builder.trust_required = trust_required(reqs)
        response_type = builder.root_type(ep.response_schema, n.response_model, n.summary)
        builder.trust_required = True
        page_param = next(
            (
                p
                for p in params
                if page and p.wire == page.get("param") and p.location == page.get("location")
            ),
            None,
        )
        spec.methods.append(
            MethodSpec(
                name=n.method_name,
                endpoint_key=ep.key,
                summary=n.summary,
                http_method=ep.method,
                host="" if ep.host == main_host else ep.host,
                path=path,
                params=params,
                response_type=response_type,
                headers=headers,
                credentials=sorted(set(method_creds)),
                pagination=page or None,
                page_param=page_param if page.get("confirmed") else None,
                replayable=ep.replayable,
            )
        )
    spec.models = builder.models
    spec.credentials = sorted(creds.values(), key=lambda c: c.arg)
    return spec


def is_allowed(ep: Endpoint, allow: set[str]) -> bool:
    labels = {
        ep.key,
        f"{ep.method} {ep.path_template}",
        f"{ep.method} {ep.host}{ep.path_template}",
        ep.label,
    }
    return bool(labels & {a.strip() for a in allow})


def _skip_reason(ep: Endpoint, reqs: list[CapturedRequest], allowed: bool = False) -> str | None:
    probe = RawRequest(ep.method, f"https://{ep.host}{ep.path_template}", {}, None)
    if not allowed and not is_replayable(probe):
        return "may change data, so no method is generated (use --allow if it only reads)"
    if not allowed and ep.replayable is False:
        return "could not be called outside the browser"
    if ep.method == "POST" and reqs and not _json_dict(reqs[-1].req_body) and reqs[-1].req_body:
        return "request body is not JSON"
    return None


def _body_params(
    reqs: list[CapturedRequest], used: set[str], page: dict[str, Any]
) -> list[ParamSpec]:
    bodies = [b for b in (_json_dict(r.req_body) for r in reqs) if b is not None]
    if not bodies:
        return []
    values: dict[str, list[Any]] = defaultdict(list)
    for body in bodies:
        for k, v in body.items():
            values[k].append(v)
    params = []
    for key, seen in values.items():
        if is_sensitive_name(key) or any(v == REDACTED for v in seen):
            continue  # never generate secrets; rare in bodies
        arg = unique(snake(key), used)
        py_type = _py_type(seen)
        literal = repr(seen[0]) if _is_literal(seen[0]) else None
        constant = len(bodies) > 1 and all(v == seen[0] for v in seen)
        is_page = page.get("location") == "body" and page.get("param") == key
        if constant or is_page or len(seen) < len(bodies):
            params.append(
                ParamSpec(
                    arg,
                    key,
                    "body",
                    py_type,
                    False,
                    default=literal if (constant or is_page) else None,
                    example=literal,
                )
            )
        elif is_user_input(key, [str(v) for v in seen]):
            params.append(ParamSpec(arg, key, "body", py_type, True, example=literal))
        else:
            # Varies between captured requests but isn't user input (a setting or
            # filter): optional, defaulting to a value seen working.
            params.append(
                ParamSpec(arg, key, "body", py_type, False, default=literal, example=literal)
            )
    return params


# Names of parameters a caller is expected to supply.
_INPUT_NAMES = {
    "q", "query", "search", "searchterm", "search_query", "term", "keyword", "keywords",
    "text", "id", "slug", "key", "sku", "username", "user", "name", "url",
}  # fmt: skip


def is_user_input(name: str, examples: list[str]) -> bool:
    """True for search terms and identifiers; False for settings like sort or filters."""
    n = snake(name)
    if n in _INPUT_NAMES or n.endswith(("_id", "_slug", "_query")):
        return True
    # ID-shaped strings (UUIDs, hashes) are inputs too; plain numbers like 1 or 30 are
    # usually settings, so they don't count on their own.
    return bool(examples) and all(
        looks_like_id(str(v)) and not str(v).lstrip("-").isdigit() for v in examples
    )


def _credential(
    creds: dict[tuple[str, str], CredentialSpec], wire: str, location: str, prefix: str
) -> str:
    key = (wire.lower(), location)
    if key not in creds:
        arg = snake(wire)
        creds[key] = CredentialSpec(arg, wire, location, f"{prefix}_{arg.upper()}")  # type: ignore[arg-type]
    return creds[key].arg


def _json_dict(body: str | None) -> dict[str, Any] | None:
    try:
        data = json.loads(body or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _literal(value: str, json_type: str) -> str:
    if json_type == "integer":
        return str(int(value))
    if json_type == "number":
        return str(float(value))
    if json_type == "boolean":
        return str(value.lower() == "true")
    return repr(value)


def _is_literal(value: Any) -> bool:
    return isinstance(value, str | int | float | bool | type(None) | list | dict)


def _py_type(values: list[Any]) -> str:
    kinds = {type(v) for v in values if v is not None}
    if kinds == {bool}:
        return "bool"
    if kinds == {int}:
        return "int"
    if kinds <= {int, float} and kinds:
        return "float"
    if kinds == {str}:
        return "str"
    if kinds == {list}:
        return "list[Any]"
    if kinds == {dict}:
        return "dict[str, Any]"
    return "Any"
