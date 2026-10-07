"""Generate a typed Python client from an endpoint map (Stage 6)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ghost_api.analysis.models import Endpoint
from ghost_api.capture.models import CapturedRequest
from ghost_api.generator.naming import NAMING_SYSTEM, NamingPlan, apply_plan, naming_prompt
from ghost_api.generator.python_gen import GeneratedClient, write
from ghost_api.generator.spec import ClientSpec, build_spec, is_allowed
from ghost_api.generator.ts_gen import write_typescript
from ghost_api.llm.gateway import LLMError, LLMGateway
from ghost_api.memory.store import Store


class NothingToGenerate(Exception):
    pass


@dataclass
class GenerationResult:
    client: GeneratedClient
    spec: ClientSpec
    plan: NamingPlan | None  # reuse it when regenerating, to save LLM calls

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
    and store the new plan (Stage 8: no LLM call when nothing changed)."""
    stored = store.load_naming_plan(domain)
    if stored:
        try:
            plan = NamingPlan.model_validate_json(stored)
        except ValueError:
            plan = None
        if plan is not None and {e.key for e in endpoints} <= {n.key for n in plan.endpoints}:
            return plan
    plan = name_endpoints(llm, domain, endpoints)
    if plan is not None:
        store.save_naming_plan(domain, plan.model_dump_json())
    return plan


def generate_from(
    domain: str,
    start_url: str,
    endpoints: list[Endpoint],
    requests: list[CapturedRequest],
    out_root: Path,
    plan: NamingPlan | None = None,
    lang: str = "python",
    allow: set[str] | None = None,
) -> GenerationResult:
    class_name, names = apply_plan(plan, endpoints, domain)
    spec = build_spec(domain, start_url, endpoints, requests, class_name, names, allow)
    if not spec.methods:
        reasons = "; ".join(spec.skipped) or "no endpoints"
        raise NothingToGenerate(f"no endpoint can be turned into a method ({reasons})")
    if lang == "typescript":
        return GenerationResult(write_typescript(spec, out_root / domain), spec, plan)
    return GenerationResult(write(spec, out_root / domain), spec, plan)


def generate_client(
    store: Store,
    domain: str,
    out_root: Path,
    llm: LLMGateway | None = None,
    lang: str = "python",
    allow: set[str] | None = None,
) -> GenerationResult:
    """Build the client for `domain` from the endpoints of its most recent run."""
    run_id = store.latest_endpoint_run(domain)
    run = store.latest_run(domain)
    if run_id is None or run is None:
        raise NothingToGenerate(f"no endpoints stored for {domain}; run `ghost-api run` first")
    endpoints = store.load_endpoints(domain, run_id)
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
    return generate_from(domain, run.start_url, endpoints, requests, out_root, plan, lang, allowed)
