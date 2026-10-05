"""Generate a typed Python client from a domain's stored endpoint map (Stage 6)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ghost_api.generator.naming import NAMING_SYSTEM, NamingPlan, apply_plan, naming_prompt
from ghost_api.generator.python_gen import GeneratedClient, write
from ghost_api.generator.spec import ClientSpec, build_spec
from ghost_api.llm.gateway import LLMError, LLMGateway
from ghost_api.memory.store import Store


class NothingToGenerate(Exception):
    pass


@dataclass
class GenerationResult:
    client: GeneratedClient
    spec: ClientSpec
    named_by_llm: bool


def generate_client(
    store: Store, domain: str, out_root: Path, llm: LLMGateway | None = None
) -> GenerationResult:
    """Build the client for `domain` from the endpoints of its most recent run."""
    run_id = store.latest_endpoint_run(domain)
    run = store.latest_run(domain)
    if run_id is None or run is None:
        raise NothingToGenerate(f"no endpoints stored for {domain}; run `ghost-api run` first")
    endpoints = store.load_endpoints(domain, run_id)
    requests = store.requests_for_domain(domain)

    plan: NamingPlan | None = None
    if llm is not None and endpoints:
        try:
            reply = llm.ask(
                [
                    {"role": "system", "content": NAMING_SYSTEM},
                    {"role": "user", "content": naming_prompt(domain, endpoints)},
                ],
                response_model=NamingPlan,
            )
            plan = reply.parsed
        except LLMError:
            plan = None  # deterministic names still work

    class_name, names = apply_plan(plan, endpoints, domain)
    spec = build_spec(domain, run.start_url, endpoints, requests, class_name, names)
    if not spec.methods:
        reasons = "; ".join(spec.skipped) or "no endpoints"
        raise NothingToGenerate(f"no endpoint can be turned into a method ({reasons})")
    client = write(spec, out_root / domain)
    return GenerationResult(client, spec, named_by_llm=plan is not None)
