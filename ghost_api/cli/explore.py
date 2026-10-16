"""Commands that explore sites: run, import, refresh, login, logout."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer

from ghost_api import exitcodes, workflows
from ghost_api.cli._common import (
    SITE_HELP,
    _check_lang,
    _need_db,
    _normalize_url,
    _not_allowed,
    _options,
    _renderer,
    _RunResult,
    _site,
    app,
    console,
    json_out,
)
from ghost_api.config import (
    Settings,
    default_db_path,
)
from ghost_api.events import (
    EventBus,
    Notice,
)
from ghost_api.memory.store import Store
from ghost_api.net import RateLimiter
from ghost_api.pipeline import (
    RunOptions,
    finish,
    open_session,
    robots_policy,
)
from ghost_api.site import site_key
from ghost_api.ui import keys


@app.command()
def run(
    url: Annotated[str, typer.Argument(help="Website to explore, e.g. https://example.com")],
    goal: Annotated[str, typer.Option("--goal", "-g", help="What the client should do.")],
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    headed: Annotated[bool, typer.Option("--headed", help="Show the browser window.")] = False,
    max_steps: Annotated[int, typer.Option(help="Maximum agent actions.")] = 40,
    max_llm_calls: Annotated[int, typer.Option(help="Maximum LLM calls.")] = 50,
    max_minutes: Annotated[float, typer.Option(help="Time limit in minutes.")] = 15.0,
    rate_limit: Annotated[
        float, typer.Option(help="Max requests per second the tool sends itself.")
    ] = 1.0,
    ignore_robots: Annotated[
        bool, typer.Option("--ignore-robots", help="Skip the robots.txt check.")
    ] = False,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    no_agent: Annotated[
        bool, typer.Option("--no-agent", help="Don't use the LLM; just load and scroll the page.")
    ] = False,
    no_probe: Annotated[
        bool, typer.Option("--no-probe", help="Don't test endpoints by calling them directly.")
    ] = False,
    max_probes: Annotated[
        int, typer.Option(help="Maximum direct API calls for testing endpoints.")
    ] = 60,
    no_generate: Annotated[
        bool, typer.Option("--no-generate", help="Don't generate a client.")
    ] = False,
    no_verify: Annotated[
        bool, typer.Option("--no-verify", help="Don't run the generated tests against the API.")
    ] = False,
    third_party_robots: Annotated[
        bool,
        typer.Option(
            "--third-party-robots",
            help="Also check the page's API calls to other sites against their robots.txt.",
        ),
    ] = False,
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
) -> None:
    """Explore a website with the AI agent and map its hidden API."""
    console.print(f"[dim]⏳ Starting run on {url}...[/]")
    try:
        settings = Settings(
            url=_normalize_url(url),
            goal=goal,
            lang=lang,
            headed=headed,
            max_steps=max_steps,
            max_llm_calls=max_llm_calls,
            max_minutes=max_minutes,
            rate_limit=rate_limit,
            ignore_robots=ignore_robots,
            db_path=db or default_db_path(),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    opts = RunOptions(
        lang=settings.lang,
        headed=settings.headed,
        max_steps=settings.max_steps,
        max_minutes=settings.max_minutes,
        max_probes=max_probes,
        no_probe=no_probe,
        no_generate=no_generate,
        no_verify=no_verify,
        out=out,
        third_party_robots=third_party_robots,
    )
    bus = EventBus()
    renderer = _renderer()
    bus.subscribe(renderer.handle)
    result = _RunResult()
    bus.subscribe(result)
    ask = renderer.ask if settings.headed and sys.stdin.isatty() else None
    try:
        workflows.run(
            settings, opts, bus, use_agent=not no_agent, ask_user=ask,
            stop=keys.stop_requested, started_hook=renderer.start,
        )  # fmt: skip
    except workflows.NotAllowed as exc:
        _not_allowed(settings.url, str(exc))
    except workflows.NoLLMKey as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=exitcodes.USAGE) from exc
    finally:
        renderer.close()
    console.print(f"[dim]Saved to {settings.db_path}[/]")
    result.finish(settings.db_path)


@app.command("import")
def import_har(
    har: Annotated[Path, typer.Argument(help="A .har file saved from the browser's DevTools.")],
    goal: Annotated[
        str, typer.Option("--goal", "-g", help="What you were doing (for the report).")
    ] = "imported from a HAR file",
    url: Annotated[
        str | None, typer.Option(help="The site's start URL (default: the first page in the file).")
    ] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline", help="Send nothing to the site: no endpoint tests, no client tests."
        ),
    ] = False,
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    rate_limit: Annotated[
        float, typer.Option(help="Max requests per second the tool sends itself.")
    ] = 1.0,
    max_probes: Annotated[
        int, typer.Option(help="Maximum direct API calls for testing endpoints.")
    ] = 60,
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Map a site's API from a HAR file you recorded in your browser (no AI, no browser).

    For sites behind a login or a bot check: browse them yourself with DevTools open
    (Network tab), "Save all as HAR", then import. Cookies, tokens and personal data are
    removed before anything is stored. Delete the .har file afterwards: it holds them.
    """
    from ghost_api.capture.har import HarError, read_har, summary
    from ghost_api.net import RobotsPolicy

    lang = _check_lang(lang)
    try:
        recording = read_har(har, _normalize_url(url) if url else None)
    except (HarError, OSError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=exitcodes.USAGE) from exc
    for line in summary(recording):
        console.print(f"[dim]{line}[/]")
    try:  # the project's rules (terms checked, denied paths) apply to imports too
        policy = workflows.enforce(site_key(recording.start_url) or recording.target_host, False)
    except workflows.NotAllowed as exc:
        _not_allowed(recording.start_url, str(exc))
        return
    if policy.deny_paths:
        denied = sum(1 for c, _ in recording.captured if policy.denies(urlsplit(c.url).path))
        recording.captured = [(c, r) for c, r in recording.captured
                              if not policy.denies(urlsplit(c.url).path)]  # fmt: skip
        if denied:
            console.print(
                f"[dim]{denied} recorded call(s) left out: denied by the project rules[/]"
            )
    path = db or default_db_path()
    bus = EventBus()
    renderer = _renderer()
    bus.subscribe(renderer.handle)
    result = _RunResult()
    bus.subscribe(result)
    limiter = RateLimiter(rate_limit)
    host = recording.target_host
    if offline:
        robots = RobotsPolicy.allow_all(host, "offline import: nothing is sent")
    else:
        robots, allowed = robots_policy(recording.start_url, False, limiter, bus)
        if not allowed:  # the recording is still mapped, just never replayed
            offline = True
            bus.emit(Notice("Importing without testing endpoints (robots.txt)", "warning"))
    opts = RunOptions(lang=lang, max_probes=max_probes, no_probe=offline, no_verify=offline,
                      out=out)  # fmt: skip
    with Store(path) as store:
        renderer.start()
        try:
            s = open_session(
                store, bus, host, recording.start_url, goal, False, limiter, robots, None,
                max_probes, screens_root=path, lang=lang,
            )  # fmt: skip
            started = time.monotonic()
            s.phase("explore")
            for captured, raw in recording.captured:
                s.recorder.add(captured, raw)
            s.recorder.dropped.update(recording.dropped)
            reason = f"imported {len(recording.captured)} API calls from {har.name}"
            finish(s, "captured", reason, started, opts)
        finally:
            renderer.close()
        console.print(f"[dim]Saved to {path}. You can delete {har.name} now.[/]")
    result.finish(path)


@app.command()
def refresh(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    headed: Annotated[bool, typer.Option("--headed", help="Show the browser window.")] = False,
    max_steps: Annotated[int, typer.Option(help="Agent steps, if the agent is needed.")] = 10,
    max_llm_calls: Annotated[int, typer.Option(help="Maximum LLM calls.")] = 20,
    rate_limit: Annotated[
        float, typer.Option(help="Max requests per second the tool sends itself.")
    ] = 1.0,
    ignore_robots: Annotated[
        bool, typer.Option("--ignore-robots", help="Skip the robots.txt check.")
    ] = False,
    max_probes: Annotated[
        int, typer.Option(help="Maximum direct API calls for testing endpoints.")
    ] = 60,
    no_verify: Annotated[
        bool, typer.Option("--no-verify", help="Don't run the generated tests against the API.")
    ] = False,
    out: Annotated[Path, typer.Option(help="Folder for generated clients.")] = Path("output"),
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Never call the LLM (no agent, URL-based names).")
    ] = False,
    third_party_robots: Annotated[
        bool,
        typer.Option(
            "--third-party-robots",
            help="Also check the page's API calls to other sites against their robots.txt.",
        ),
    ] = False,
    lang: Annotated[str, typer.Option(help="Client language: python or typescript.")] = "python",
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Re-check a known site using memory: replay first, browse only if needed.

    Also continues an interrupted run, since its traffic and endpoints were saved.
    """
    console.print(f"[dim]⏳ Starting refresh for {domain}...[/]")
    path = _need_db(db)
    lang = _check_lang(lang)
    with Store(path) as store:
        domain = _site(store, domain)
    opts = RunOptions(lang=lang, headed=headed, max_steps=max_steps, max_probes=max_probes,
                      no_verify=no_verify, out=out,
                      third_party_robots=third_party_robots)  # fmt: skip
    bus = EventBus()
    renderer = _renderer()
    bus.subscribe(renderer.handle)
    result = _RunResult()
    bus.subscribe(result)
    try:
        _, now, first = workflows.refresh(
            path, domain, opts, bus, use_llm=not no_llm, max_llm_calls=max_llm_calls,
            rate_limit=rate_limit, ignore_robots=ignore_robots, stop=keys.stop_requested,
            started_hook=renderer.start,
        )  # fmt: skip
    except workflows.NothingRemembered as exc:
        console.print(str(exc))
        raise typer.Exit(code=exitcodes.FAILED) from exc
    except workflows.NotAllowed as exc:
        _not_allowed(domain, str(exc))
    finally:
        renderer.close()
    if first:
        saved = 100 * (first - now) / first
        console.print(
            f"[bold]Memory:[/] {now} LLM call(s) vs {first} on the first run ({saved:.0f}% fewer)"
        )
    result.data["llm_calls"] = now
    result.finish(path)


@app.command()
def login(
    url: Annotated[
        str | None, typer.Argument(help="The site's page to log in on, e.g. https://example.com")
    ] = None,
    list_saved: Annotated[bool, typer.Option("--list", help="Show saved logins.")] = False,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Log in to a site yourself, once; Ghost API reuses the session.

    A browser window opens: log in as you normally would (Ghost API never sees your
    password), then press Enter here. Runs and refreshes then start logged in, and fetch,
    watch and serve send the session's cookies. Remove it with `ghost-api logout SITE`.
    """
    from ghost_api.browser import auth_state
    from ghost_api.browser.driver import ActionError, BrowserSession
    from ghost_api.capture.recorder import TrafficRecorder
    from ghost_api.ui import keys

    path = db or default_db_path()
    if list_saved or url is None:
        saved = auth_state.saved_sites(path)
        if _options["json"]:
            json_out({"logins": saved})
        elif not saved:
            console.print("No saved logins. Save one with: ghost-api login https://example.com")
        for meta in [] if _options["json"] else saved:
            age = auth_state.age_days(meta)
            how = "encrypted" if meta.get("encrypted") else "owner-only file"
            console.print(f"  {meta['site']}: saved {age:.0f} day(s) ago, "
                          f"{meta.get('cookies', '?')} cookies, {how}")  # fmt: skip
        return
    url = _normalize_url(url)
    site = site_key(url) or (urlsplit(url).hostname or "")
    host = urlsplit(url).hostname or ""
    console.print(f"Opening {url}. Log in in the browser window, then come back here.")
    with BrowserSession(url, TrafficRecorder(host), headed=True) as browser:
        try:
            browser.goto(url)
        except ActionError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=exitcodes.FAILED) from exc
        with keys.input_paused():
            typer.prompt("Press Enter when you are logged in", default="", show_default=False)
        state = browser.export_state()
    meta = auth_state.save(path, site, state)
    if _options["json"]:
        json_out(meta)
        return
    how = (
        "encrypted for your Windows account"
        if meta["encrypted"]
        else "in a file only your user can read (not encrypted)"
    )
    console.print(
        f"[green]Saved the login for {site}[/] ({meta['cookies']} cookies, {how}). "
        f"Remove it with: ghost-api logout {site}"
    )


@app.command()
def logout(
    domain: Annotated[str, typer.Argument(help=SITE_HELP)],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Delete a site's saved login."""
    from ghost_api.browser import auth_state

    path = db or default_db_path()
    names = [m["site"] for m in auth_state.saved_sites(path)]
    site = domain if domain in names else next((n for n in names if n.startswith(domain)), domain)
    removed = auth_state.delete(path, site)
    if _options["json"]:
        json_out({"site": site, "removed": removed})
    elif removed:
        console.print(f"Deleted the saved login for {site}.")
    else:
        console.print(f"No saved login for {domain}.")
