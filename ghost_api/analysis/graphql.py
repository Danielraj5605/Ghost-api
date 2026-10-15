"""GraphQL: one URL, many operations. Each named operation becomes its own endpoint.

A site's GraphQL traffic all goes to `/graphql`, but `GetUser` and `ListOrders` are
different calls with different inputs and replies. The operation name is added to the
endpoint's template after a `#` (`POST /graphql#GetUser`), so every later stage (schemas,
testing, naming, the client) treats each operation as one endpoint. The `#...` part is
never sent: it is a URL fragment.

Mutations change data: they are never replayed or generated (probe/replay.py).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

_NAMED_OP = re.compile(r"^\s*(query|mutation|subscription)\s+([A-Za-z_]\w*)", re.M)
_OP_NAME = re.compile(r"^[A-Za-z_]\w{0,80}$")


def graphql_payload(method: str, url: str, body: str | None) -> dict[str, Any] | None:
    """The GraphQL request (query, operationName, variables), or None if it isn't one.
    A batch (a list of operations) is not split: it stays one endpoint."""
    if method.upper() == "GET":
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        if "query" in params and ("{" in params["query"] or "operationName" in params):
            variables = params.get("variables")
            try:
                parsed = json.loads(variables) if variables else {}
            except ValueError:
                parsed = {}
            return {**params, "variables": parsed if isinstance(parsed, dict) else {}}
        return None
    try:
        data = json.loads(body or "")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    has_query = isinstance(data.get("query"), str) and "{" in data["query"]
    extensions = data.get("extensions")
    persisted = isinstance(extensions, dict) and "persistedQuery" in extensions
    if not (has_query or persisted):
        return None
    return data


def operation_name(payload: dict[str, Any]) -> str | None:
    name = payload.get("operationName")
    if isinstance(name, str) and _OP_NAME.match(name):
        return name
    match = _NAMED_OP.search(str(payload.get("query") or ""))
    return match.group(2) if match else None


def operation_of(method: str, url: str, body: str | None) -> str | None:
    """'GetUser' for a GraphQL request with a named operation, else None."""
    payload = graphql_payload(method, url, body)
    return operation_name(payload) if payload is not None else None


def split_template(template: str) -> tuple[str, str | None]:
    """'/graphql#GetUser' -> ('/graphql', 'GetUser'); other templates are unchanged."""
    path, _, op = template.partition("#")
    return path, op or None
