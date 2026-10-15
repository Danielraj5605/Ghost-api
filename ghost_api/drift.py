"""API drift: what changed in a site's API, how much it matters, and the client's version.

A *snapshot* is what a client user sees: each method (its call, parameters and return
type) and each model (its fields and their types). Snapshots are stored per site with a
version number. Comparing two snapshots gives a list of changes:

    breaking   code using the client can stop working: a method or field is gone, a type
               changed, a parameter became required or disappeared
    additive   something new: a method, an optional parameter, a field, a model

The client's version follows semantic versioning: breaking -> major, additive -> minor,
anything else (a description, an example) -> patch. The first version is 1.0.0.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ghost_api.generator.spec import ClientSpec
from ghost_api.memory.store import Store

Severity = Literal["breaking", "additive", "info"]
FIRST_VERSION = "1.0.0"


@dataclass(frozen=True)
class Change:
    severity: Severity
    where: str  # "get_quotes", "Quote.author"
    text: str  # plain words: "returns QuotesResponse instead of list[Quote]"

    def line(self) -> str:
        mark = {"breaking": "!", "additive": "+", "info": "~"}[self.severity]
        return f"{mark} {self.where}: {self.text}"


def snapshot(spec: ClientSpec) -> dict[str, Any]:
    """The client's public shape, as plain JSON (stable order, so equal APIs compare equal)."""
    return {
        "methods": {
            m.name: {
                "call": f"{m.http_method} {m.host or ''}{m.path}",
                "params": {p.name: {"type": p.type, "required": p.required} for p in m.params},
                "returns": m.response_type,
                "summary": m.summary,
            }
            for m in sorted(spec.methods, key=lambda m: m.name)
        },
        "models": {
            md.name: {f.name: {"type": f.type, "optional": f.optional} for f in md.fields}
            for md in sorted(spec.models, key=lambda md: md.name)
        },
    }


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    changes: list[Change] = []
    old_m, new_m = old.get("methods", {}), new.get("methods", {})
    for name in sorted(old_m.keys() - new_m.keys()):
        changes.append(Change("breaking", name, f"method removed ({old_m[name]['call']})"))
    for name in sorted(new_m.keys() - old_m.keys()):
        changes.append(Change("additive", name, f"new method ({new_m[name]['call']})"))
    for name in sorted(old_m.keys() & new_m.keys()):
        changes += _method_changes(name, old_m[name], new_m[name])
    old_d, new_d = old.get("models", {}), new.get("models", {})
    for name in sorted(old_d.keys() - new_d.keys()):
        changes.append(Change("breaking", name, "model removed"))
    for name in sorted(new_d.keys() - old_d.keys()):
        changes.append(Change("additive", name, "new model"))
    for name in sorted(old_d.keys() & new_d.keys()):
        changes += _field_changes(name, old_d[name], new_d[name])
    return changes


def _method_changes(name: str, old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    out: list[Change] = []
    if old["call"] != new["call"]:
        out.append(Change("info", name, f"now calls {new['call']} (was {old['call']})"))
    if old["returns"] != new["returns"]:
        out.append(
            Change("breaking", name, f"returns {new['returns']} instead of {old['returns']}")
        )
    if old.get("summary") != new.get("summary"):
        out.append(Change("info", name, "description changed"))
    old_p, new_p = old["params"], new["params"]
    for p in sorted(old_p.keys() - new_p.keys()):
        out.append(Change("breaking", f"{name}({p})", "parameter removed"))
    for p in sorted(new_p.keys() - old_p.keys()):
        if new_p[p]["required"]:
            out.append(Change("breaking", f"{name}({p})", "new required parameter"))
        else:
            out.append(Change("additive", f"{name}({p})", "new optional parameter"))
    for p in sorted(old_p.keys() & new_p.keys()):
        a, b = old_p[p], new_p[p]
        if a["type"] != b["type"]:
            text = f"type {b['type']} instead of {a['type']}"
            out.append(Change("breaking", f"{name}({p})", text))
        if not a["required"] and b["required"]:
            out.append(Change("breaking", f"{name}({p})", "is now required"))
        elif a["required"] and not b["required"]:
            out.append(Change("additive", f"{name}({p})", "is now optional"))
    return out


def _field_changes(model: str, old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    out: list[Change] = []
    for f in sorted(old.keys() - new.keys()):
        out.append(Change("breaking", f"{model}.{f}", "field removed"))
    for f in sorted(new.keys() - old.keys()):
        out.append(Change("additive", f"{model}.{f}", f"new field ({new[f]['type']})"))
    for f in sorted(old.keys() & new.keys()):
        a, b = old[f]["type"], new[f]["type"]
        if a == b:
            continue
        where = f"{model}.{f}"
        # Reply data: a field that can no longer be None is safe for code reading it;
        # one that may now be None is not.
        if _without_none(a) == _without_none(b):
            if _nullable(a) and not _nullable(b):
                out.append(Change("info", where, "is now always present"))
            else:
                out.append(Change("breaking", where, f"may now be missing ({b})"))
        else:
            out.append(Change("breaking", where, f"type {b} instead of {a}"))
    return out


def _nullable(type_text: str) -> bool:
    return "None" in [part.strip() for part in type_text.split("|")]


def _without_none(type_text: str) -> str:
    return " | ".join(p.strip() for p in type_text.split("|") if p.strip() != "None")


def bump(version: str, changes: list[Change]) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if any(c.severity == "breaking" for c in changes):
        return f"{major + 1}.0.0"
    if any(c.severity == "additive" for c in changes):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


@dataclass
class VersionInfo:
    version: str
    changes: list[Change]
    previous: str | None  # the version this one follows, None for the first
    is_new: bool  # differs from the latest stored version
    history: list[dict[str, Any]]  # stored versions, newest first (for changelogs)

    def summary(self) -> str:
        if self.previous is None:
            return f"first version {self.version}"
        if not self.is_new:
            return f"no change since {self.version}"
        counts = {s: sum(c.severity == s for c in self.changes) for s in ("breaking", "additive")}
        parts = [f"{n} {s}" for s, n in counts.items() if n]
        return f"{self.previous} -> {self.version} ({', '.join(parts) or 'small changes'})"


def version_for(store: Store, domain: str, spec: ClientSpec) -> VersionInfo:
    """The version `spec` has (or would get), compared with the latest stored one."""
    history = store.api_versions(domain)
    snap = snapshot(spec)
    if not history:
        return VersionInfo(FIRST_VERSION, [], None, True, history)
    latest = history[0]
    if latest["snapshot"] == snap:
        return VersionInfo(latest["version"], [], latest["version"], False, history)
    changes = compare(latest["snapshot"], snap)
    return VersionInfo(bump(latest["version"], changes), changes, latest["version"], True, history)


def record(store: Store, domain: str, spec: ClientSpec, run_id: int | None) -> VersionInfo:
    """Store `spec` as a new version when it differs from the latest one."""
    info = version_for(store, domain, spec)
    if info.is_new:
        store.add_api_version(
            domain, info.version, snapshot(spec), [asdict(c) for c in info.changes], run_id
        )
    return info


def changelog(spec: ClientSpec, info: VersionInfo) -> str:
    """CHANGELOG.md for a client package: every stored version, plus this one if new."""
    lines = [
        f"# Changelog: {spec.class_name}",
        "",
        f"Versions of the unofficial {spec.domain} client, written by Ghost API when the "
        "site's API changed. `!` breaking, `+` added, `~` other.",
        "",
    ]
    entries = list(info.history)
    if info.is_new:
        entries.insert(
            0,
            {"version": info.version, "created_at": "unreleased",
             "changes": [asdict(c) for c in info.changes]},
        )  # fmt: skip
    for entry in entries:
        lines += [f"## {entry['version']} ({str(entry['created_at'])[:10]})", ""]
        changes = [Change(**c) for c in entry["changes"]]
        lines += [f"- {c.line()}" for c in changes] or ["- First version."]
        lines.append("")
    return "\n".join(lines)


def to_markdown(site: str, info: VersionInfo, changes: list[Change]) -> str:
    """A pull-request body for `ghost-api diff --markdown`."""
    if not changes:
        return f"No API changes for **{site}** (client {info.version})."
    breaking = [c for c in changes if c.severity == "breaking"]
    lines = [
        f"## API changes on {site}",
        "",
        f"Client version **{info.previous or '-'} -> {info.version}**"
        + (f": **{len(breaking)} breaking**" if breaking else ""),
        "",
    ]
    for title, severity in (("Breaking", "breaking"), ("Added", "additive"), ("Other", "info")):
        group = [c for c in changes if c.severity == severity]
        if group:
            lines += [f"### {title}", ""] + [f"- `{c.where}`: {c.text}" for c in group] + [""]
    lines.append("Generated by [Ghost API](https://github.com/Danielraj5605/Ghost-api).")
    return "\n".join(lines)


def changes_json(changes: list[Change]) -> list[dict[str, Any]]:
    return [asdict(c) for c in changes]


def dumps(snap: dict[str, Any]) -> str:
    return json.dumps(snap, sort_keys=True)


# --- live check: compare a fresh reply with the stored response schema ---------------------


@dataclass(frozen=True)
class FieldShape:
    types: frozenset[str]
    required: bool  # present in every captured reply


def schema_fields(schema: dict[str, Any] | None, max_depth: int = 4) -> dict[str, FieldShape]:
    """{"quotes[].author.name": FieldShape({"string"}, True), ...} for a JSON schema."""
    out: dict[str, FieldShape] = {}

    def types(node: dict[str, Any]) -> frozenset[str]:
        kind = node.get("type")
        found = set(kind) if isinstance(kind, list) else {kind} if kind else set()
        for option in node.get("anyOf", []):
            found |= types(option)
        return frozenset(t for t in found if t)

    def walk(node: dict[str, Any], prefix: str, required: bool, depth: int) -> None:
        kinds = types(node)
        if prefix:
            out[prefix] = FieldShape(kinds, required)
        if depth >= max_depth:
            return
        if "array" in kinds and isinstance(node.get("items"), dict):
            walk(node["items"], prefix + "[]", required, depth + 1)
        needed = set(node.get("required", []))
        for key, child in (node.get("properties") or {}).items():
            if isinstance(child, dict):
                path = f"{prefix}.{key}" if prefix else key
                walk(child, path, required and key in needed, depth + 1)
        for option in node.get("anyOf", []):
            if isinstance(option, dict) and option.get("properties"):
                walk({**option, "type": None}, prefix, required, depth)

    if schema:
        walk(schema, "", True, 0)
    return out


def live_changes(
    where: str, stored: dict[str, Any] | None, fresh: dict[str, Any] | None
) -> list[Change]:
    """How a fresh reply differs from what was captured before. A field missing from one
    reply is only breaking when every earlier reply had it."""
    before, now = schema_fields(stored), schema_fields(fresh)
    out: list[Change] = []
    for path in sorted(before.keys() - now.keys()):
        if before[path].required and "[]" not in path.rstrip("[]"):
            out.append(Change("breaking", f"{where} {path}", "field missing from the reply"))
    for path in sorted(now.keys() - before.keys()):
        out.append(Change("additive", f"{where} {path}", "new field in the reply"))
    for path in sorted(before.keys() & now.keys()):
        old, new = before[path].types - {"null"}, now[path].types - {"null"}
        if old and new and not (new <= old or {"integer", "number"} >= old | new):
            text = f"now {'/'.join(sorted(new))}, was {'/'.join(sorted(old))}"
            out.append(Change("breaking", f"{where} {path}", text))
    return out
