"""Phase 1-2: events, the UI-free pipeline, and the terminal renderer."""

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from evals.demo_shop import serve
from ghost_api.agent.tools import Decision
from ghost_api.events import (
    AgentStep,
    ClientGenerated,
    EndpointsUpdated,
    Event,
    EventBus,
    HypothesisTested,
    Notice,
    PhaseChanged,
    RequestCaptured,
    RunFinished,
    RunStarted,
    Screenshot,
    UsageUpdated,
    VerifyRound,
)
from ghost_api.llm.gateway import LLMGateway, LLMReply
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter, RobotsPolicy
from ghost_api.pipeline import RunOptions, explore_site, finish, open_session
from ghost_api.ui.terminal import TerminalRenderer

# --- events ---


def test_event_to_dict_has_a_type() -> None:
    assert Notice("hi", "warning").to_dict() == {
        "type": "notice",
        "text": "hi",
        "level": "warning",
        "detail": "",
    }
    assert PhaseChanged("test").to_dict()["type"] == "phase"


def test_bus_delivers_in_order_and_survives_failing_subscribers() -> None:
    bus = EventBus()
    seen: list[str] = []

    def broken(event: Event) -> None:
        raise RuntimeError("renderer crashed")

    bus.subscribe(broken)
    bus.subscribe(lambda e: seen.append(e.kind))
    bus.emit(Notice("a"))
    bus.emit(PhaseChanged("explore"))
    assert seen == ["notice", "phase"]
    assert len(bus.errors) == 2 and "renderer crashed" in bus.errors[0]


def test_store_keeps_events_in_order(tmp_path: Path) -> None:
    with Store(tmp_path / "t.db") as store:
        run_id = store.create_run("x.test", "https://x.test/", "g")
        store.add_event(run_id, Notice("one").to_dict())
        store.add_event(run_id, PhaseChanged("test").to_dict())
        events = store.events_for_run(run_id)
        assert [(e["seq"], e["type"]) for e in events] == [(1, "notice"), (2, "phase")]
        assert store.events_for_run(run_id, after=1)[0]["phase"] == "test"


# --- the pipeline on the demo shop (no LLM: plain visit) ---


def test_pipeline_emits_and_stores_the_whole_run(tmp_path: Path) -> None:
    shop = serve(0)
    url = f"http://127.0.0.1:{shop.server_address[1]}/"
    db = tmp_path / "t.db"
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    opts = RunOptions(out=tmp_path / "out")
    try:
        with Store(db) as store:
            s = open_session(
                store,
                bus,
                "127.0.0.1",
                url,
                "list products",
                False,
                RateLimiter(50),
                RobotsPolicy.allow_all("127.0.0.1"),
                None,
                30,
                screens_root=db,
            )
            status, reason = explore_site(s, opts, use_agent=False)
            finish(s, status, reason, 0.0, opts)
            stored = store.events_for_run(s.run_id)
    finally:
        shop.shutdown()

    kinds = [e.kind for e in seen]
    assert kinds[0] == "run_started" and kinds[-1] == "phase"
    phases = [e.phase for e in seen if isinstance(e, PhaseChanged)]
    assert phases == ["explore", "test", "generate", "verify", "learn", "done"]
    assert any(isinstance(e, RequestCaptured) for e in seen)
    assert any(isinstance(e, HypothesisTested) and e.result == "confirmed" for e in seen)
    final = next(e for e in seen if isinstance(e, EndpointsUpdated) and e is not None)
    assert {ep["label"] for ep in final.endpoints} >= {"GET /api/products", "GET /api/categories"}
    assert any(isinstance(e, ClientGenerated) for e in seen)
    done = next(e for e in seen if isinstance(e, RunFinished))
    assert done.status == "captured" and Path(done.report).exists()
    # Everything that was shown is in the database, in the same order.
    assert [e["type"] for e in stored] == kinds
    # The plain visit saved a screenshot for the live view.
    shot = next(e for e in seen if e.kind == "screenshot")
    assert (tmp_path / "t_files" / "runs" / str(seen[0].run_id) / shot.name).exists()  # type: ignore[attr-defined]


class FinishAtOnce(LLMGateway):
    """An agent that decides the goal is already covered at step 1 (no quota used)."""

    def __init__(self) -> None:
        super().__init__([])

    def ask(self, messages: Any, response_model: Any = None, temperature: float = 0.0) -> Any:
        decision = Decision(thought="Already covered", action="finish", goal_covered=True)
        self.usage.answered += 1
        return LLMReply("", "fake", "m", False, decision)


def test_agent_run_that_finishes_at_once_still_shows_the_page(tmp_path: Path) -> None:
    """Live check found it: the start screenshot was saved but never announced."""
    shop = serve(0)
    url = f"http://127.0.0.1:{shop.server_address[1]}/"
    db = tmp_path / "t.db"
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    opts = RunOptions(out=tmp_path / "out", no_probe=True, no_generate=True, no_verify=True)
    try:
        with Store(db) as store:
            s = open_session(
                store,
                bus,
                "127.0.0.1",
                url,
                "list products",
                False,
                RateLimiter(50),
                RobotsPolicy.allow_all("127.0.0.1"),
                FinishAtOnce(),
                30,
                screens_root=db,
            )
            explore_site(s, opts, use_agent=True)
    finally:
        shop.shutdown()

    shots = [e for e in seen if isinstance(e, Screenshot)]
    assert [e.name for e in shots] == ["start.jpg"]
    assert shots[0].url.startswith(url)
    assert (tmp_path / "t_files" / "runs" / str(s.run_id) / "start.jpg").exists()
    step = next(e for e in seen if isinstance(e, AgentStep))
    assert step.action == "finish"


# --- the terminal renderer ---


def sample_run() -> list[Event]:
    endpoint = {
        "key": "GET x/api/quotes",
        "label": "GET /api/quotes",
        "method": "GET",
        "status": "verified",
        "confidence": "High",
        "auth_type": "none",
        "pagination": {"style": "page", "param": "page", "confirmed": True},
        "fields": "quotes[], page",
    }
    return [
        RunStarted(7, "quotes.toscrape.com", "https://quotes.toscrape.com/", "list quotes"),
        PhaseChanged("explore"),
        RequestCaptured("GET", "https://quotes.toscrape.com/api/quotes?page=1", 200),
        AgentStep(
            1,
            "scroll",
            {"direction": "bottom"},
            "Load more quotes",
            "1 new request",
            ["GET /api/quotes"],
            1,
        ),
        PhaseChanged("test"),
        HypothesisTested("GET /api/quotes", "paginates with 'page'", "confirmed", "10 new"),
        EndpointsUpdated([endpoint]),
        PhaseChanged("verify"),
        VerifyRound(1, 2, 0, 0),
        ClientGenerated(
            "QuotesClient",
            "python",
            True,
            "output/quotes.toscrape.com",
            [
                {
                    "name": "list_quotes",
                    "args": [],
                    "returns": "QuotesPage",
                    "iterator": "iter_list_quotes",
                }
            ],
            files=["quotes_client.py", "models.py"],
        ),
        UsageUpdated(answered=2, tokens=1714),
        RunFinished(
            "goal-covered",
            "quotes, authors and tags covered",
            32.4,
            2,
            1,
            report="output/quotes.toscrape.com/report.md",
        ),
    ]


def render(plain: bool) -> str:
    console = Console(record=True, force_terminal=not plain, width=110, color_system=None)
    renderer = TerminalRenderer(console, plain=plain)
    renderer.start()
    for event in sample_run():
        renderer.handle(event)
    renderer.close()
    return console.export_text()


def test_live_terminal_looks_like_claude_code() -> None:
    text = render(plain=False)
    for expected in [
        "◆ Ghost API",
        "⏺ Exploring the site",
        "⎿ Step 1 scroll",
        "+ GET",
        "⏺ Testing endpoints",
        "✓ paginates with 'page'",
        "Round 1",
        "2 passed",
        "goal-covered",
        "QuotesClient",
        "list_quotes() -> QuotesPage",
        "iter_list_quotes",
        "Use it",
        "from quotes_client import QuotesClient",
        "ghost-api ui",
    ]:
        assert expected in text, expected


def test_plain_terminal_keeps_simple_lines() -> None:
    text = render(plain=True)
    assert "Step 1 scroll" in text and "+ new endpoint GET /api/quotes" in text
    assert "Finished: goal-covered" in text
    assert "⏺" not in text and "◆" not in text


def test_renderer_status_line_tracks_progress() -> None:
    console = Console(record=True, force_terminal=True, width=110)
    renderer = TerminalRenderer(console, plain=False)
    for event in sample_run()[:5]:
        renderer.handle(event)
    status = Console(width=110, record=True, color_system=None)
    status.print(renderer._status())
    line = status.export_text()
    assert "✓ Explore" in line and "◐ Test" in line and "○ Verify" in line
    assert "1 request ·" in line


# --- the CLI ---


def test_no_command_shows_help_when_not_interactive() -> None:
    from ghost_api.cli import app

    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0 and "run" in result.output and "ui" in result.output


def test_plain_flag_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ghost_api.cli import app

    result = CliRunner().invoke(app, ["--plain", "inspect", "--db", str(tmp_path / "none.db")])
    assert "No database" in result.output
