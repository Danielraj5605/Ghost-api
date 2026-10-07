"""Group concrete URLs into path templates (FR-17).

    /api/books/1, /api/books/2           -> /api/books/{id}
    /users/7/posts/19                    -> /users/{user_id}/posts/{id}
    /author/albert-einstein, /author/... -> /author/{slug}

Two rules decide that a path segment is a parameter:
  1. It looks like an identifier on its own (number, UUID, hex hash, date, long token).
  2. It varies across URLs that are identical everywhere else, and the values look
     like slugs, or there are many distinct values. Plain words such as /api/quotes
     vs /api/authors stay literal, because they are usually different endpoints.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from ghost_api.analysis.models import ParamType

_INT_RE = re.compile(r"^-?\d+$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RE = re.compile(r"^(?=.*\d)[0-9a-f]{16,}$", re.I)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Long mixed tokens like Mongo/Firebase/base64 IDs: letters AND digits, 20+ chars, no dashes.
_TOKEN_RE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_]{20,}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+([-_.][a-z0-9]+)+$", re.I)
_VERSION_RE = re.compile(r"^v\d+(\.\d+)*$", re.I)
# Encoded data rather than a name: "pb=!1m8!3m7..." (Google Maps tiles), "@12.9,77.6,15z",
# matrix parameters ("items;page=2"), or anything very long.
_OPAQUE_RE = re.compile(r"[!=;,@]")
OPAQUE_LENGTH = 48
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,30})=")

# A varying segment with at least this many distinct values becomes a parameter even
# when the values are plain words.
HIGH_CARDINALITY = 6


def split_path(path: str) -> list[str]:
    return [unquote(s) for s in path.split("/") if s]


def looks_like_id(segment: str) -> bool:
    if _VERSION_RE.match(segment):
        return False
    return bool(
        _INT_RE.match(segment)
        or _UUID_RE.match(segment)
        or _HEX_RE.match(segment)
        or _DATE_RE.match(segment)
        or _TOKEN_RE.match(segment)
        or is_opaque(segment)
    )


def is_opaque(segment: str) -> bool:
    return len(segment) > OPAQUE_LENGTH or bool(_OPAQUE_RE.search(segment))


def _is_leading_version(pos: int, segment: str, length: int) -> bool:
    return pos == 0 and length > 1 and segment.isdigit() and len(segment) <= 2


def looks_like_slug(segment: str) -> bool:
    if _VERSION_RE.match(segment):
        return False
    return bool(_SLUG_RE.match(segment)) or any(c.isdigit() for c in segment)


def infer_type(values: list[str]) -> ParamType:
    if values and all(_INT_RE.match(v) for v in values):
        return "integer"
    if values and all(re.match(r"^-?\d+\.\d+$", v) or _INT_RE.match(v) for v in values):
        return "number"
    if values and all(v.lower() in ("true", "false") for v in values):
        return "boolean"
    return "string"


_INVARIANT = {"news", "series", "species", "data", "media", "settings", "metrics"}


def singular(word: str) -> str:
    word = re.sub(r"[^a-zA-Z0-9]+", "_", word).strip("_").lower()
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("xes", "ches", "shes", "sses", "zes")):
        return word[:-2]
    if word in _INVARIANT or word.endswith(("us", "is", "ss")):
        return word  # status, analysis, address, news
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


@dataclass
class _Shape:
    """URLs sharing method, host, segment count and trailing slash."""

    method: str
    host: str
    trailing_slash: bool
    urls: list[str]
    segments: list[list[str]]


@dataclass(frozen=True)
class TemplateMatch:
    """Where one concrete URL landed: its template and the path-parameter values."""

    method: str
    host: str
    template: str
    param_names: tuple[str, ...]
    param_values: tuple[str, ...]


def group_urls(requests: list[tuple[str, str]]) -> list[TemplateMatch]:
    """Map each (method, url) to a template. Output order matches input order."""
    shapes: dict[tuple[str, str, int, bool], _Shape] = {}
    placement: list[tuple[tuple[str, str, int, bool], int]] = []
    for method, url in requests:
        parts = urlsplit(url)
        segs = split_path(parts.path)
        trailing = parts.path.endswith("/") and bool(segs)
        key = (method.upper(), (parts.hostname or "").lower(), len(segs), trailing)
        shape = shapes.setdefault(key, _Shape(key[0], key[1], trailing, [], []))
        placement.append((key, len(shape.urls)))
        shape.urls.append(url)
        shape.segments.append(segs)

    templated = {key: _template_shape(shape) for key, shape in shapes.items()}
    return [templated[key][i] for key, i in placement]


def _template_shape(shape: _Shape) -> list[TemplateMatch]:
    # Rule 1: each segment that looks like an ID is a parameter, URL by URL. A short number
    # as the first of several segments is an API version (/1/indexes/...), not an ID.
    masks = [
        [
            looks_like_id(seg) and not _is_leading_version(p, seg, len(segs))
            for p, seg in enumerate(segs)
        ]
        for segs in shape.segments
    ]
    n = len(shape.segments[0]) if shape.segments else 0

    # Rule 2: among URLs identical everywhere except `pos`, a varying slug-like or
    # high-cardinality segment is a parameter.
    for pos in range(n):
        groups: dict[tuple[str | None, ...], list[int]] = defaultdict(list)
        for i, segs in enumerate(shape.segments):
            if masks[i][pos]:
                continue
            key = tuple(None if masks[i][p] else segs[p] for p in range(n) if p != pos)
            groups[key].append(i)
        for key, members in groups.items():
            distinct = {shape.segments[i][pos] for i in members}
            anchored = any(k is not None for k in key)
            if len(distinct) > 1 and (
                (anchored and all(looks_like_slug(v) for v in distinct))
                or len(distinct) >= HIGH_CARDINALITY
            ):
                for i in members:
                    masks[i][pos] = True

    # Name parameters per final template, using that template's own values.
    by_skeleton: dict[tuple[str | None, ...], list[int]] = defaultdict(list)
    for i, segs in enumerate(shape.segments):
        by_skeleton[tuple(None if masks[i][p] else segs[p] for p in range(n))].append(i)

    results: list[TemplateMatch | None] = [None] * len(shape.segments)
    for skeleton, members in by_skeleton.items():
        names = _param_names(list(skeleton), [shape.segments[i] for i in members])
        out = [("{" + names[p] + "}") if seg is None else seg for p, seg in enumerate(skeleton)]
        template = "/" + "/".join(out) + ("/" if shape.trailing_slash else "")
        param_pos = [p for p, seg in enumerate(skeleton) if seg is None]
        for i in members:
            results[i] = TemplateMatch(
                method=shape.method,
                host=shape.host,
                template=template,
                param_names=tuple(names[p] for p in param_pos),
                param_values=tuple(shape.segments[i][p] for p in param_pos),
            )
    return [r for r in results if r is not None]


def _param_names(skeleton: list[str | None], samples: list[list[str]]) -> list[str]:
    """Name parameters after the literal segment before them: /users/{user_id}/posts/{id}."""
    names = [""] * len(skeleton)
    param_positions = [p for p, seg in enumerate(skeleton) if seg is None]
    used: set[str] = set()
    for idx, pos in enumerate(param_positions):
        prev = skeleton[pos - 1] if pos > 0 else None
        values = [segs[pos] for segs in samples]
        keys = {m.group(1) for v in values if (m := _KEY_RE.match(v))}
        if len(keys) == 1 and all(_KEY_RE.match(v) for v in values):
            name = keys.pop().lower()  # /maps/vt/{pb} for "pb=!1m8..."
        elif idx == len(param_positions) - 1:
            if all(looks_like_id(v) for v in values):
                name = "id"
            else:  # /author/{author}, /indexes/{index}
                name = singular(prev) if prev else "slug"
        elif prev:
            name = f"{singular(prev)}_id"
        else:
            name = f"param{idx + 1}"
        base, k = name, 2
        while name in used:
            name, k = f"{base}{k}", k + 1
        used.add(name)
        names[pos] = name
    return names
