"""The local web dashboard: `ghost-api ui`.

A FastAPI app on 127.0.0.1 that serves the React frontend (built into `static/`) and a
JSON API over the same database and output folder the CLI uses.

Security, since this runs on your machine:
  - It listens on 127.0.0.1 only.
  - Every /api request needs the random token made at launch. The token is embedded in
    the page the server sends; other websites can't read that page, so they can't send
    requests here even though your browser can reach the port.
  - The Host header must be this server (blocks DNS-rebinding tricks), and requests that
    change something must come from this origin.
  - API keys and unredacted requests never leave the server.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import secrets
import threading
import time
import webbrowser
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field
from rich.console import Console

from ghost_api import __version__
from ghost_api.analysis.analyzer import endpoint_keys
from ghost_api.config import SUPPORTED_LANGUAGES
from ghost_api.errors import friendly_error
from ghost_api.events import EventBus, Notice
from ghost_api.export.site_data import SAFE_NAME, endpoint_details, run_dict, site_files
from ghost_api.generator import NothingToGenerate, generate_client
from ghost_api.generator.python_gen import GenerationError
from ghost_api.llm.doctor import check_providers
from ghost_api.llm.gateway import LLMGateway
from ghost_api.llm.providers import default_providers
from ghost_api.memory.knowledge import load_memory, memory_prompt, rebuild_raw
from ghost_api.memory.store import RunRow, Store
from ghost_api.net import RateLimiter
from ghost_api.pipeline import (
    RunOptions,
    client_event,
    explore_site,
    finish,
    open_session,
    robots_policy,
    screenshots_dir,
)
from ghost_api.probe.replay import Replayer, ReplayRefused, apply_params, is_replayable
from ghost_api.site import from_url_id, is_site, site_host, to_url_id

STATIC = Path(__file__).parent / "static"
TOKEN_PLACEHOLDER = "__GHOST_TOKEN__"
SCREEN_NAME = re.compile(r"^[A-Za-z0-9_-]+\.jpg$")
MAX_TRY_BODY = 200_000

GatewayFactory = Callable[[int], LLMGateway | None]


def default_gateway(max_calls: int) -> LLMGateway | None:
    gateway = LLMGateway(default_providers(), max_calls=max_calls)
    return gateway if gateway.providers else None


class StartRun(BaseModel):
    url: str = Field(min_length=3, max_length=2000)
    goal: str = Field(min_length=1, max_length=500)
    lang: str = "python"
    agent: bool = True
    max_steps: int = Field(20, ge=1, le=60)
    max_llm_calls: int = Field(30, ge=1, le=100)
    ignore_robots: bool = False


class TryCall(BaseModel):
    endpoint: str  # Endpoint.key
    params: dict[str, str] = Field(default_factory=dict)


class Regenerate(BaseModel):
    lang: str = "python"


# --- running one exploration at a time ------------------------------------------------------


class RunManager:
    def __init__(self, db: Path, out: Path, gateway_factory: GatewayFactory) -> None:
        self.db = db
        self.out = out
        self.gateway_factory = gateway_factory
        self.active_run: int | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, req: StartRun, timeout: float = 60) -> int:
        with self._lock:
            if self.busy:
                raise HTTPException(409, "A run is already in progress. Stop it or wait.")
            if req.lang not in SUPPORTED_LANGUAGES:
                raise HTTPException(400, f"Unsupported language: {req.lang}")
            ready = threading.Event()
            result: dict[str, Any] = {}
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, args=(req, ready, result), daemon=True, name="ghost-run"
            )
            self._thread.start()
        if not ready.wait(timeout):
            raise HTTPException(504, "The run did not start in time.")
        if "error" in result:
            raise HTTPException(400, result["error"])
        return int(result["run_id"])

    def stop(self, run_id: int) -> bool:
        if self.active_run != run_id:
            return False
        self._stop.set()
        return True

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, req: StartRun, ready: threading.Event, result: dict[str, Any]) -> None:
        bus = EventBus()
        notices: list[Notice] = []
        bus.subscribe(lambda e: notices.append(e) if isinstance(e, Notice) else None)
        try:
            url = req.url if "://" in req.url else f"https://{req.url}"
            host = (urlsplit(url).hostname or "").lower()
            if not host or urlsplit(url).scheme not in ("http", "https"):
                result["error"] = f"Not a valid http(s) URL: {req.url}"
                return
            limiter = RateLimiter(1.0)
            robots, allowed = robots_policy(url, req.ignore_robots, limiter, bus)
            if not allowed:
                result["error"] = notices[-1].text if notices else "Disallowed by robots.txt"
                return
            gateway = self.gateway_factory(req.max_llm_calls) if req.agent else None
            if req.agent and gateway is None:
                result["error"] = (
                    "No LLM API key found. Add one to .env, or turn off the AI agent to only "
                    "load and scroll the page."
                )
                return
            with Store(self.db) as store:
                s = open_session(
                    store,
                    bus,
                    host,
                    url,
                    req.goal,
                    False,
                    limiter,
                    robots,
                    gateway,
                    60,
                    screens_root=self.db,
                    lang=req.lang,
                )
                s.stop = self._stop.is_set
                for notice in notices:  # the robots.txt result, now that the run is recorded
                    store.add_event(s.run_id, notice.to_dict())
                known = load_memory(store, host)
                if known is not None and known.run_id != s.run_id:
                    s.memory = memory_prompt(known.endpoints, known.lessons)
                    bus.emit(
                        Notice(
                            f"Using memory: {len(known.endpoints)} known endpoints, "
                            f"{len(known.lessons)} lessons"
                        )
                    )
                self.active_run = s.run_id
                result["run_id"] = s.run_id
                ready.set()
                opts = RunOptions(
                    lang=req.lang, max_steps=req.max_steps, max_minutes=10, out=self.out
                )
                started = time.monotonic()
                status, reason = explore_site(s, opts, use_agent=gateway is not None)
                finish(s, status, reason, started, opts)
        except Exception as exc:  # noqa: BLE001 - report it to the page, keep serving
            result.setdefault("error", friendly_error(exc))
        finally:
            self.active_run = None
            ready.set()


# --- reading what is stored -----------------------------------------------------------------


def _site_name(url_id: str) -> str:
    """The site from its URL form ('www.google.com~maps' -> 'www.google.com/maps')."""
    site = from_url_id(url_id)
    if not is_site(site):
        raise HTTPException(400, "Invalid site name")
    return site


def _site_folder(out: Path, site: str) -> Path:
    if not is_site(site):
        raise HTTPException(400, "Invalid site name")
    return out / site


def _site_files(out: Path, domain: str) -> list[dict[str, Any]]:
    return site_files(_site_folder(out, domain))


# --- the app --------------------------------------------------------------------------------


def create_app(
    db: Path,
    out: Path,
    token: str,
    manager: RunManager | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    manager = manager or RunManager(db, out, default_gateway)
    app = FastAPI(title="Ghost API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.headers.get("host", "")
        if allowed_hosts is not None and host not in allowed_hosts:
            return JSONResponse({"detail": "Unknown host"}, status_code=403)
        if request.url.path.startswith("/api/"):
            given = request.headers.get("x-ghost-token") or request.query_params.get("token", "")
            if not secrets.compare_digest(given, token):
                return JSONResponse({"detail": "Missing or wrong token"}, status_code=401)
            origin = request.headers.get("origin")
            if request.method != "GET" and origin and urlsplit(origin).netloc != host:
                return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
        return await call_next(request)

    def store() -> Store:
        return Store(db)

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return {
            "version": __version__,
            "running": manager.active_run,
            "has_llm": any(p.api_key for p in default_providers()),
            "languages": list(SUPPORTED_LANGUAGES),
            "db": str(db),
            "out": str(out),
        }

    @app.get("/api/sites")
    def sites() -> list[dict[str, Any]]:
        if not db.exists():
            return []
        with store() as s:
            runs = s.list_runs()
            by_domain: dict[str, list[RunRow]] = {}
            for r in runs:
                by_domain.setdefault(r.domain, []).append(r)
            result = []
            for domain, domain_runs in by_domain.items():
                endpoints = endpoint_details(s, domain)
                counts = {
                    k: sum(e["status"] == k for e in endpoints)
                    for k in ("verified", "unverified", "failed", "discovered")
                }
                result.append(
                    {
                        "domain": domain,
                        "latest": run_dict(domain_runs[0]),
                        "runs": len(domain_runs),
                        "endpoints": len(endpoints),
                        "counts": counts,
                        "methods": sorted({e["method"] for e in endpoints}),
                        "has_client": any(f["kind"] == "client" for f in _site_files(out, domain)),
                    }
                )
        return result

    @app.get("/api/sites/{domain}")
    def site(domain: str) -> dict[str, Any]:
        domain = _site_name(domain)
        _site_folder(out, domain)
        with store() as s:
            runs = s.list_runs(domain)
            if not runs:
                raise HTTPException(404, f"Nothing stored for {domain}")
            return {
                "domain": domain,
                "runs": [run_dict(r) for r in runs],
                "endpoints": endpoint_details(s, domain),
                "lessons": s.lessons_for(domain, limit=20),
                "files": _site_files(out, domain),
            }

    @app.get("/api/runs/{run_id}")
    def run(run_id: int) -> dict[str, Any]:
        with store() as s:
            r = s.get_run(run_id)
            if r is None:
                raise HTTPException(404, "Run not found")
            return {
                **run_dict(r),
                "active": manager.active_run == run_id,
                "steps": s.steps_for_run(run_id),
                "hypotheses": s.hypotheses_for_run(run_id),
            }

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: int, after: int = 0) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last = after
            idle = 0
            while True:
                with store() as s:
                    batch = s.events_for_run(run_id, after=last)
                finished = False
                for event in batch:
                    last = int(event["seq"])
                    finished = finished or event["type"] == "run_finished"
                    yield f"id: {last}\ndata: {json.dumps(event)}\n\n"
                idle = 0 if batch else idle + 1
                if finished or (manager.active_run != run_id and not batch and idle > 2):
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.4)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    @app.post("/api/runs")
    async def start_run(req: StartRun) -> dict[str, int]:
        run_id = await asyncio.to_thread(manager.start, req)
        return {"run_id": run_id}

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: int) -> dict[str, bool]:
        return {"stopping": manager.stop(run_id)}

    @app.get("/api/runs/{run_id}/screens/{name}")
    def screen(run_id: int, name: str) -> FileResponse:
        if not SCREEN_NAME.match(name):
            raise HTTPException(400, "Invalid screenshot name")
        path = screenshots_dir(db, run_id) / name
        if not path.is_file():
            raise HTTPException(404, "No such screenshot")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/sites/{domain}/files")
    def files(domain: str) -> list[dict[str, Any]]:
        domain = _site_name(domain)
        return _site_files(out, domain)

    @app.get("/api/sites/{domain}/files/{name}")
    def file(domain: str, name: str) -> PlainTextResponse:
        domain = _site_name(domain)
        folder = _site_folder(out, domain)
        if not SAFE_NAME.match(name) or name.startswith("."):
            raise HTTPException(400, "Invalid file name")
        path = (folder / name).resolve()
        if path.parent != folder.resolve() or not path.is_file():
            raise HTTPException(404, "No such file")
        return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))

    @app.get("/api/sites/{domain}/download.zip")
    def download(domain: str) -> Response:
        domain = _site_name(domain)
        folder = _site_folder(out, domain)
        listing = _site_files(out, domain)
        if not listing:
            raise HTTPException(404, "No files for this site yet")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in listing:
                zf.write(folder / f["name"], f"{domain}/{f['name']}")
        filename = to_url_id(domain).replace("~", "-")
        headers = {"Content-Disposition": f'attachment; filename="{filename}.zip"'}
        return Response(buffer.getvalue(), media_type="application/zip", headers=headers)

    @app.post("/api/sites/{domain}/generate")
    def regenerate(domain: str, body: Regenerate) -> dict[str, Any]:
        domain = _site_name(domain)
        _site_folder(out, domain)
        if body.lang not in SUPPORTED_LANGUAGES:
            raise HTTPException(400, f"Unsupported language: {body.lang}")
        try:
            with store() as s:
                result = generate_client(s, domain, out, llm=None, lang=body.lang)
        except (NothingToGenerate, GenerationError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return client_event(result).to_dict()

    @app.post("/api/sites/{domain}/try")
    def try_endpoint(domain: str, call: TryCall) -> dict[str, Any]:
        """Call an endpoint once, read-only, with the given parameters."""
        domain = _site_name(domain)
        _site_folder(out, domain)
        with store() as s:
            run_id = s.latest_endpoint_run(domain)
            endpoints = s.load_endpoints(domain, run_id) if run_id else []
            target = next((e for e in endpoints if e.key == call.endpoint), None)
            if target is None:
                raise HTTPException(404, "Unknown endpoint")
            stored = s.requests_for_domain(domain)
        samples = [
            r for k, r in zip(endpoint_keys(stored), stored, strict=False) if k == target.key
        ]
        sample = None
        for captured in reversed(samples):
            raw = rebuild_raw(captured, target.required_headers)
            if raw is not None and is_replayable(raw):
                sample = raw
                break
        if sample is None:
            raise HTTPException(
                409,
                "This endpoint needs a fresh key or session, or it changes data. Run a "
                "refresh first, or use the generated client with your credentials.",
            )
        request = apply_params(
            sample, target.path_template, {p.name for p in target.path_params}, call.params
        )
        replayer = Replayer(site_host(domain), RateLimiter(1.0), max_requests=1)
        started = time.monotonic()
        try:
            result = replayer.send(request)
        except ReplayRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            replayer.close()
        text = json.dumps(result.body, indent=2) if result.body is not None else ""
        return {
            "status": result.status,
            "ok": result.ok,
            "error": result.error,
            "url": result.captured.url if result.captured else "",
            "ms": round((time.monotonic() - started) * 1000),
            "body": text[:MAX_TRY_BODY],
            "truncated": len(text) > MAX_TRY_BODY,
        }

    @app.post("/api/doctor")
    async def doctor() -> list[dict[str, Any]]:
        return await asyncio.to_thread(check_providers)

    # --- the frontend ---

    def index() -> HTMLResponse:
        page = STATIC / "index.html"
        if not page.is_file():
            return HTMLResponse(
                "<h1>Ghost API</h1><p>The dashboard frontend is not built. Run "
                "<code>npm run build</code> in the <code>web/</code> folder.</p>",
                status_code=503,
            )
        html = page.read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/assets/{name}")
    def asset(name: str) -> FileResponse:
        if not SAFE_NAME.match(name):
            raise HTTPException(404)
        path = STATIC / "assets" / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.get("/{path:path}")
    def spa(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404)
        static_file = STATIC / path
        if path and SAFE_NAME.match(path) and static_file.is_file():
            return FileResponse(static_file)  # favicon, logo...
        return index()  # client-side routes

    return app


def launch_token() -> str:
    """A new random token per launch, or GHOST_API_UI_TOKEN for frontend development
    (`npm run dev` sends the same value as VITE_GHOST_TOKEN)."""
    return os.getenv("GHOST_API_UI_TOKEN") or secrets.token_urlsafe(24)


def serve(
    db: Path, out: Path, port: int, open_browser: bool, console: Console | None = None
) -> None:
    import uvicorn

    console = console or Console()
    token = launch_token()
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app = create_app(db, out, token, allowed_hosts=hosts)
    url = f"http://127.0.0.1:{port}/"
    console.print(f"[bold #a78bfa]◆ Ghost API dashboard[/]  {url}")
    console.print("[dim]Local only. Press Ctrl+C to stop.[/]")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
