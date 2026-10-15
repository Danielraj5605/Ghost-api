"""What Ghost API remembers about a site between runs (Stage 8).

- Endpoints (with probe results and statuses) and their redacted samples: already in
  the store from earlier stages.
- Lessons: short notes for future runs. "auto" lessons are written by code (which action
  revealed which endpoint), "llm" lessons by one optional LLM call per run.
- The naming plan, so regenerating a client needs no LLM call.

Lessons come from web content and LLM output, so they are treated as untrusted hints:
length-capped, single-line, redacted, and labelled as possibly outdated in prompts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ghost_api.analysis.models import Endpoint
from ghost_api.capture.filters import REDACTED, redact_text
from ghost_api.capture.models import CapturedRequest, RawRequest
from ghost_api.generator.naming import NamingPlan
from ghost_api.llm.gateway import LLMError, LLMGateway
from ghost_api.memory.store import Store

MAX_LESSON_CHARS = 200
MAX_LESSONS_IN_PROMPT = 6


@dataclass
class DomainMemory:
    domain: str
    goal: str
    start_url: str
    run_id: int  # the run whose endpoints these are
    endpoints: list[Endpoint]
    lessons: list[str] = field(default_factory=list)
    plan: NamingPlan | None = None


def load_memory(store: Store, domain: str) -> DomainMemory | None:
    run_id = store.latest_endpoint_run(domain)
    runs = [r for r in store.list_runs(domain) if not r.goal.startswith("refresh:")]
    if run_id is None or not runs:
        return None
    plan_json = store.load_naming_plan(domain)
    plan = None
    if plan_json:
        try:
            plan = NamingPlan.model_validate_json(plan_json)
        except ValueError:
            plan = None
    return DomainMemory(
        domain=domain,
        goal=runs[0].goal,
        start_url=runs[0].start_url,
        run_id=run_id,
        endpoints=store.current_endpoints(domain),
        lessons=store.lessons_for(domain),
        plan=plan,
    )


def clean_lesson(text: str) -> str:
    """One line, no secrets, bounded length."""
    text = re.sub(r"\s+", " ", redact_text(text)).strip()
    return text[:MAX_LESSON_CHARS]


def memory_prompt(endpoints: Sequence[Endpoint], lessons: Sequence[str]) -> str:
    """Compact text for the agent's prompt, or '' when there is nothing to say."""
    if not endpoints and not lessons:
        return ""
    lines = ["Earlier runs on this site (may be outdated; the current page is the truth):"]
    for ep in list(endpoints)[:15]:
        status = f" [{ep.status}]" if ep.status not in ("", "discovered") else ""
        lines.append(f"- known endpoint: {ep.method} {ep.host}{ep.path_template}{status}")
    for lesson in list(lessons)[:MAX_LESSONS_IN_PROMPT]:
        lines.append(f"- note: {lesson}")
    return "\n".join(lines)


def discovery_lessons(steps: Sequence[tuple[str, dict[str, object], list[str]]]) -> list[str]:
    """Deterministic lessons: which action revealed which endpoint.

    `steps` are (action, args, new endpoint labels) tuples from the agent's log.
    """
    lessons = []
    for action, args, new in steps:
        if not new or action in ("finish", "ask_user"):
            continue
        shown = ", ".join(f"{k}={v!r}" for k, v in args.items() if k != "element_id")
        how = f"{action}({shown})" if shown else action
        for label in new:
            lessons.append(clean_lesson(f"{label} appeared after {how}"))
    return lessons


class Lessons(BaseModel):
    lessons: list[str] = Field(
        description="1 to 3 short, specific tips for exploring this site next time.",
        max_length=3,
    )


LESSONS_SYSTEM = """\
You review a finished exploration of a website's hidden API and write 1 to 3 short,
specific tips that would make the next exploration of the same site faster: which page
or action reveals which endpoint, what to avoid, what needs a login. No generic advice."""


def write_lessons(
    llm: LLMGateway,
    goal: str,
    status: str,
    steps: Sequence[tuple[str, dict[str, object], str]],
    endpoints: Sequence[Endpoint],
) -> list[str]:
    """One LLM call; returns [] if the LLM is unavailable."""
    history = "\n".join(
        f"{i}. {action}({json.dumps(args)}) -> {result[:160]}"
        for i, (action, args, result) in enumerate(steps, 1)
    )
    found = "\n".join(f"- {e.method} {e.host}{e.path_template}" for e in endpoints) or "(none)"
    prompt = f"GOAL: {goal}\nOUTCOME: {status}\n\nSTEPS:\n{history}\n\nENDPOINTS FOUND:\n{found}"
    try:
        reply = llm.ask(
            [{"role": "system", "content": LESSONS_SYSTEM}, {"role": "user", "content": prompt}],
            response_model=Lessons,
        )
    except LLMError:
        return []
    if reply.parsed is None:
        return []
    return [c for c in (clean_lesson(t) for t in reply.parsed.lessons) if c]


def rebuild_raw(captured: CapturedRequest, required_headers: Sequence[str]) -> RawRequest | None:
    """A replayable request from a stored (redacted) sample, when nothing needed is missing.

    Returns None if a redacted value would be needed: a secret in the URL or body, or a
    redacted header that probing found to be required. Fresh traffic is needed then.
    """
    if REDACTED in captured.url or (captured.req_body and REDACTED in captured.req_body):
        return None
    required = {h.lower() for h in required_headers}
    headers = {}
    for name, value in captured.req_headers.items():
        if value == REDACTED:
            if name.lower() in required:
                return None
            continue  # e.g. a cookie that turned out not to matter
        headers[name] = value
    return RawRequest(captured.method, captured.url, headers, captured.req_body)
