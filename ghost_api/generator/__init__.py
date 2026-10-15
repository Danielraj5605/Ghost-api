"""Generate a typed Python client from an endpoint map (Stage 6)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ghost_api.analysis.models import Endpoint
from ghost_api.capture.models import CapturedRequest
from ghost_api.generator.naming import (
    NAMING_SYSTEM,
    NamingPlan,
    apply_plan,
    default_client_name,
    naming_prompt,
)
from ghost_api.generator.python_gen import GeneratedClient, write
from ghost_api.generator.spec import ClientSpec, build_spec, is_allowed
from ghost_api.generator.ts_gen import write_typescript
from ghost_api.llm.gateway import LLMError, LLMGateway
from ghost_api.memory.store import Store

if TYPE_CHECKING:
    from ghost_api.drift import VersionInfo

__all__ = [
    "GenerationResult",
    "NothingToGenerate",
    "generate_client",
    "generate_from",
    "name_endpoints",
    "plan_for",
    "site_spec",
]


class NothingToGenerate(Exception):
    pass


@dataclass
class GenerationResult:
    client: GeneratedClient
    spec: ClientSpec
    plan: NamingPlan | None  # reuse it when regenerating, to save LLM calls
    version: VersionInfo | None = None  # with a store: the client's version and changes

    @property
    def named_by_llm(self) -> bool:
        return self.plan is not None


def name_endpoints(
    llm: LLMGateway | None, domain: str, endpoints: list[Endpoint]
) -> NamingPlan | None:
    """One LLM call for nicer names. None if there's no LLM or it fails."""
    if llm is None or not endpoints:
        return None
    try:
        reply = llm.ask(
            [
                {"role": "system", "content": NAMING_SYSTEM},
                {"role": "user", "content": naming_prompt(domain, endpoints)},
            ],
            response_model=NamingPlan,
        )
    except LLMError:
        return None  # deterministic names still work
    return reply.parsed


def plan_for(
    store: Store, llm: LLMGateway | None, domain: str, endpoints: list[Endpoint]
) -> NamingPlan | None:
    """Reuse the stored naming plan if it covers every endpoint; otherwise ask the LLM once
    and store the new plan (Stage 8: no LLM call when nothing changed).

    Once a client version exists, names are part of its public API: existing names never
    change (a rename would break code using the client); only new endpoints get names."""
    stored = store.load_naming_plan(domain)
    plan: NamingPlan | None = None
    if stored:
        try:
            plan = NamingPlan.model_validate_json(stored)
        except ValueError:
            plan = None
        if plan is not None and {e.key for e in endpoints} <= {n.key for n in plan.endpoints}:
            return plan
    if store.api_versions(domain):
        return _name_new_endpoints(store, llm, domain, endpoints, plan)
    plan = name_endpoints(llm, domain, endpoints)
    if plan is not None:
        store.save_naming_plan(domain, plan.model_dump_json())
    return plan


def _name_new_endpoints(
    store: Store,
    llm: LLMGateway | None,
    domain: str,
    endpoints: list[Endpoint],
    plan: NamingPlan | None,
) -> NamingPlan | None:
    """A released client keeps every name it has; the LLM names only what is new."""
    named = {n.key for n in plan.endpoints} if plan else set()
    if plan is None:
        # The released client used names derived from URLs: keep those for what exists.
        previous = store.api_versions(domain)[0]["snapshot"]["methods"]
        calls = {m["call"] for m in previous.values()}
        named = {e.key for e in endpoints if f"{e.method} {e.path_template}" in calls}
        named |= {e.key for e in endpoints if any(c.endswith(e.path_template) for c in calls)}
    new = [e for e in endpoints if e.key not in named]
    fresh = name_endpoints(llm, domain, new) if new else None
    if fresh is None:
        return plan
    base = plan or NamingPlan(client_class=default_client_name(domain), endpoints=[])
    merged = NamingPlan(
        client_class=base.client_class,
        endpoints=[*base.endpoints, *(n for n in fresh.endpoints if n.key not in named)],
    )
    store.save_naming_plan(domain, merged.model_dump_json())
    return merged


def generate_from(
    domain: str,
    start_url: str,
    endpoints: list[Endpoint],
    requests: list[CapturedRequest],
    out_root: Path,
    plan: NamingPlan | None = None,
    lang: str = "python",
    allow: set[str] | None = None,
    store: Store | None = None,
) -> GenerationResult:
    """`store`: compare with the stored client versions, so the packages carry the right
    version and changelog (nothing is recorded here; see drift.record)."""
    class_name, names = apply_plan(plan, endpoints, domain)
    spec = build_spec(domain, start_url, endpoints, requests, class_name, names, allow)
    if not spec.methods:
        reasons = "; ".join(spec.skipped) or "no endpoints"
        raise NothingToGenerate(f"no endpoint can be turned into a method ({reasons})")
    info = None
    if store is not None:
        from ghost_api import drift  # drift imports this package

        info = drift.version_for(store, domain, spec)
        spec.version, spec.changelog = info.version, drift.changelog(spec, info)
    if lang == "typescript":
        return GenerationResult(write_typescript(spec, out_root / domain), spec, plan, info)
    return GenerationResult(write(spec, out_root / domain), spec, plan, info)


def generate_client(
    store: Store,
    domain: str,
    out_root: Path,
    llm: LLMGateway | None = None,
    lang: str = "python",
    allow: set[str] | None = None,
) -> GenerationResult:
    """Build the client for `domain` from everything known about it (all runs)."""
    run_id = store.latest_endpoint_run(domain)
    run = store.latest_run(domain)
    if run_id is None or run is None:
        raise NothingToGenerate(f"no endpoints stored for {domain}; run `ghost-api run` first")
    endpoints = store.current_endpoints(domain)
    requests = store.requests_for_domain(domain)
    plan = plan_for(store, llm, domain, endpoints)
    # Remember what the user allowed, so a later plain `generate` keeps those methods.
    if allow:
        unknown = {a for a in allow if not any(is_allowed(e, {a}) for e in endpoints)}
        if unknown:
            names = ", ".join(sorted(unknown))
            raise NothingToGenerate(f"--allow names no stored endpoint: {names}")
        store.allow_endpoints(domain, [e.key for e in endpoints if is_allowed(e, allow)])
    allowed = store.allowed_endpoints(domain)
    result = generate_from(
        domain, run.start_url, endpoints, requests, out_root, plan, lang, allowed, store
    )
    from ghost_api import drift

    result.version = drift.record(store, domain, result.spec, run.id)
    return result


def site_spec(store: Store, domain: str) -> ClientSpec:
    """The client `generate` would build from what is stored now, without writing files
    or calling an LLM (the stored naming plan is reused)."""
    run = store.latest_run(domain)
    endpoints = store.current_endpoints(domain)
    if run is None or not endpoints:
        raise NothingToGenerate(f"no endpoints stored for {domain}; run `ghost-api run` first")
    plan = None
    stored = store.load_naming_plan(domain)
    if stored:
        try:
            plan = NamingPlan.model_validate_json(stored)
        except ValueError:
            plan = None
    class_name, names = apply_plan(plan, endpoints, domain)
    requests = store.requests_for_domain(domain)
    allowed = store.allowed_endpoints(domain)
    return build_spec(domain, run.start_url, endpoints, requests, class_name, names, allowed)
