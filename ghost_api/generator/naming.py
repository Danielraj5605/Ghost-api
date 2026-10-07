"""Names for generated code: safe Python identifiers, with or without the LLM.

Deterministic names always work (get_quotes, QuotesResponse). One optional LLM call
(FR-22) can replace them with better ones (search_items, SearchResults); every name it
returns is validated, and anything unusable falls back to the deterministic name.
"""

from __future__ import annotations

import ipaddress
import keyword
import re

from pydantic import BaseModel, Field

from ghost_api.analysis.models import Endpoint
from ghost_api.analysis.schema import field_paths
from ghost_api.analysis.url_grouping import singular

_VERSION_RE = re.compile(r"^(v\d+(\.\d+)*|\d{1,2})$", re.I)
_NOISE_SEGMENTS = {"api", "rest", "json", "public", "ajax", "www"}


def snake(name: str) -> str:
    """'nbHits' -> 'nb_hits', 'x-algolia-agent' -> 'x_algolia_agent', '2fa' -> 'n_2fa'."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower() or "value"
    if name[0].isdigit():
        name = f"n_{name}"
    if keyword.iskeyword(name) or name in ("self", "cls"):
        name += "_"
    return name


def pascal(name: str) -> str:
    """'quotes response' / 'quotes_response' -> 'QuotesResponse'."""
    parts = re.split(r"[^0-9a-zA-Z]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name))
    out = "".join(p[:1].upper() + p[1:] for p in parts if p) or "Model"
    return out if not out[0].isdigit() else f"M{out}"


def is_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


MAX_NAME_WORDS = 3
MAX_WORD_CHARS = 30


def path_words(endpoint: Endpoint) -> list[str]:
    """Meaningful literal segments: /api/v1/books/{id}/reviews -> ['books', 'reviews']."""
    words = []
    for seg in endpoint.path_template.strip("/").split("/"):
        if not seg or seg.startswith("{") or seg.lower() in _NOISE_SEGMENTS:
            continue
        if _VERSION_RE.match(seg):
            continue
        words.append(snake(seg)[:MAX_WORD_CHARS].rstrip("_"))
    # Keep names readable: the last few words say what the endpoint is.
    return words[-MAX_NAME_WORDS:]


def default_method_name(endpoint: Endpoint) -> str:
    words = path_words(endpoint) or [snake(endpoint.host.split(".")[0])]
    verb = {"GET": "get", "HEAD": "head", "POST": "post"}.get(
        endpoint.method, endpoint.method.lower()
    )
    if endpoint.path_template.rstrip("/").endswith("}") and words:
        words[-1] = singular(words[-1])  # /books/{id} -> get_book
    return snake(f"{verb}_{'_'.join(words)}")


def default_model_name(endpoint: Endpoint) -> str:
    words = path_words(endpoint) or [endpoint.host.split(".")[0]]
    base = pascal("_".join(words))
    if endpoint.path_template.rstrip("/").endswith("}"):
        base = pascal(singular(words[-1]))
    return f"{base}Response"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def default_client_name(domain: str) -> str:
    """'quotes.toscrape.com' -> 'QuotesToscrapeClient'; 'hn.algolia.com' -> 'HnAlgoliaClient'."""
    host = domain.lower().split(":")[0].strip("[]")
    if host == "localhost" or _is_ip(host):
        return "LocalClient"  # '127.0.0.1' would otherwise give 'M1270Client'
    labels = [p for p in domain.lower().split(".") if p not in ("www", "com", "org", "net", "io")]
    return pascal("_".join(labels[:2]) or "api") + "Client"


def unique(name: str, used: set[str]) -> str:
    out, n = name, 2
    while out in used:
        out, n = f"{name}{n}", n + 1
    used.add(out)
    return out


# --- optional LLM naming pass ----------------------------------------------------------------


class EndpointNames(BaseModel):
    key: str = Field(description="The endpoint key exactly as given.")
    method_name: str = Field(description="snake_case Python method name, e.g. search_stories.")
    response_model: str = Field(description="PascalCase model name, e.g. SearchResults.")
    summary: str = Field(description="One short sentence: what this endpoint returns.")


class NamingPlan(BaseModel):
    client_class: str = Field(description="PascalCase client class name ending in Client.")
    endpoints: list[EndpointNames]


NAMING_SYSTEM = """\
You name the parts of a generated Python API client. Names must be short, clear and
describe what the endpoint does for a user (search_stories, get_quote, list_tags), not
how the URL looks. Use snake_case for methods and PascalCase for models."""


def naming_prompt(domain: str, endpoints: list[Endpoint]) -> str:
    lines = [f"Website: {domain}", "", "Endpoints:"]
    for ep in endpoints:
        params = [p.name for p in ep.path_params] + [p.name for p in ep.query_params]
        fields = ", ".join(field_paths(ep.response_schema, limit=12)) or "-"
        lines.append(f"- key: {ep.key}\n  params: {', '.join(params) or '-'}\n  returns: {fields}")
    return "\n".join(lines)


def apply_plan(
    plan: NamingPlan | None, endpoints: list[Endpoint], domain: str
) -> tuple[str, dict[str, EndpointNames]]:
    """Validated names for the client and every endpoint, falling back where needed."""
    client = default_client_name(domain)
    if plan and is_identifier(plan.client_class) and plan.client_class[:1].isupper():
        client = (
            plan.client_class
            if plan.client_class.endswith("Client")
            else f"{plan.client_class}Client"
        )
    suggested = {e.key: e for e in plan.endpoints} if plan else {}
    used_methods: set[str] = set()
    used_models: set[str] = {client}
    names: dict[str, EndpointNames] = {}
    for ep in endpoints:
        s = suggested.get(ep.key)
        method = snake(s.method_name) if s and s.method_name else default_method_name(ep)
        model = s.response_model if s and is_identifier(s.response_model) else ""
        model = pascal(model) if model else default_model_name(ep)
        summary = (s.summary.strip() if s else "") or f"{ep.method} {ep.path_template}"
        names[ep.key] = EndpointNames(
            key=ep.key,
            method_name=unique(method, used_methods),
            response_model=unique(model, used_models),
            summary=summary.replace('"""', "'''")[:200],
        )
    return client, names
