"""Stage 10: the evaluation harness and the demo shop."""

import argparse
import re
from pathlib import Path
from typing import Any

import pytest

from evals.demo_shop import ShopHandler, serve
from evals.run_eval import (
    Result,
    Site,
    generated_operations,
    load_sites,
    matches,
    normalize,
    run_site,
    score,
    to_markdown,
)
from ghost_api.agent.orchestrator import Explorer
from ghost_api.agent.tools import Decision
from ghost_api.analysis.models import Endpoint
from ghost_api.browser.driver import BrowserSession
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.llm.gateway import LLMReply

# --- matching and scoring ---


def test_normalize_and_matches() -> None:
    assert normalize("GET /api/products/{id}/reviews") == "GET /api/products/{}/reviews"
    assert matches("GET /api/products/{product_id}", "GET /api/products/{}")
    assert matches("POST x-dsn.algolia.net/1/indexes/{index}/query", "POST */1/indexes/{}/query")
    assert not matches("POST /1/indexes/{index}/query", "POST */1/indexes/{}/query")  # own host
    assert not matches("GET /api/products", "POST /api/products")


def test_score() -> None:
    site = Site(
        "s",
        "http://x",
        "g",
        expected=["GET /api/a", "GET /api/b/{}"],
        real=["POST /api/cart"],
        forbidden=["POST /api/cart"],
    )
    eps = [
        Endpoint("GET", "x", "/api/a", status="verified"),
        Endpoint("GET", "x", "/api/noise", status="failed"),
        Endpoint("POST", "x", "/api/cart"),
    ]
    s = score(site, eps, "x", generated=["POST /api/cart"])
    assert s["recall"] == 0.5
    assert s["precision"] == pytest.approx(2 / 3)
    assert s["invented"] == ["GET /api/noise"]
    assert (s["verified"], s["failed"], s["pass_rate"]) == (1, 1, 0.5)
    assert s["forbidden_generated"] == ["POST /api/cart"]


def test_score_for_a_site_without_an_api() -> None:
    s = score(Site("books", "http://x", "g", expected=[]), [], "x", generated=[])
    assert (s["recall"], s["precision"], s["pass_rate"]) == (None, 1.0, None)


def test_generated_operations(tmp_path: Path) -> None:
    spec = tmp_path / "openapi.yaml"
    spec.write_text(
        "paths:\n  /api/items/{id}:\n    get: {}\n  /1/query:\n    post:\n"
        "      servers: [{url: 'https://search.example.net'}]\n",
        encoding="utf-8",
    )
    assert generated_operations(spec) == ["GET /api/items/{}", "POST search.example.net/1/query"]
    assert generated_operations(tmp_path / "missing.yaml") == []


def test_sites_file_and_markdown() -> None:
    sites = load_sites(None)
    assert {s.name for s in sites} >= {"demo-shop", "quotes", "books-no-api"}
    assert load_sites(["quotes"])[0].url.startswith("https://")
    with pytest.raises(SystemExit):
        load_sites(["nope"])
    r = Result("demo", "goal-covered", ["GET /a"], 1.0, 1.0, [], 1, 0, 0, 1.0, 3, 12.0, 400.0, [])
    args = argparse.Namespace(max_steps=20, max_llm_calls=30, no_agent=False)
    assert "| demo | goal-covered | 100% | 100% | 100% | 3 | 12s | 400 MB | - | - |" in (
        to_markdown([r], args)
    )


# --- the whole pipeline on the demo shop, through the CLI, without the LLM ---


def test_harness_runs_the_cli_on_the_demo_shop(tmp_path: Path) -> None:
    site = next(s for s in load_sites(["demo-shop"]))
    args = argparse.Namespace(max_llm_calls=5, max_steps=5, timeout=600, no_agent=True)
    result = run_site(site, args, tmp_path)
    assert result.status == "captured", result.log
    assert result.found == ["GET /api/categories", "GET /api/products"]  # loaded by the page
    assert result.precision == 1.0 and result.invented == []  # noise filtered
    assert result.pass_rate == 1.0 and result.forbidden_generated == []


# --- every goal endpoint is reachable through the UI, and the trap is avoided ---


class LabelPicker:
    """A scripted agent that finds elements by label in the prompt (no LLM quota)."""

    def __init__(self, plan: list[tuple[str, str, dict[str, Any]]]) -> None:
        self.plan = plan
        self.prompts: list[str] = []

    def ask(
        self, messages: list[dict[str, str]], response_model: Any = None, temperature: float = 0.0
    ) -> LLMReply[Decision]:
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        action, label, extra = self.plan.pop(0)
        data: dict[str, Any] = {"thought": f"{action} {label}", "action": action, **extra}
        if label:
            match = re.search(rf'\[(\d+)\] [\w:]+ "{re.escape(label)}', prompt)
            assert match, f"{label!r} not in prompt:\n{prompt}"
            data["element_id"] = int(match.group(1))
        return LLMReply("", "fake", "m", False, Decision.model_validate(data))


def test_demo_shop_endpoints_are_reachable_and_the_cart_is_never_touched() -> None:
    server = serve(0)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    ShopHandler.calls = []
    llm = LabelPicker(
        [
            ("click", "Load more", {}),
            ("click", "Games", {}),
            ("type", "Search products", {"text": "book", "submit": True}),
            ("click", "Books item 3", {}),
            ("click", "More reviews", {}),
            ("finish", "", {"goal_covered": True}),
        ]
    )
    recorder = TrafficRecorder("127.0.0.1")
    try:
        with BrowserSession(url, recorder) as browser:
            result = Explorer(
                browser, llm, lambda: recorder.captured, "shop", url, "127.0.0.1", max_steps=10
            ).run()
    finally:
        server.shutdown()
    labels = {e.label for e in result.endpoints}
    assert labels == {
        "GET /api/categories",
        "GET /api/products",
        "GET /api/products/{id}",
        "GET /api/products/{id}/reviews",
        "GET /api/search",
    }
    for prompt in llm.prompts:  # unsafe controls were never offered
        elements = prompt.split("Elements:", 1)[1]
        assert "Add to cart" not in elements and "Log out" not in elements
    assert "POST /api/cart" not in ShopHandler.calls
