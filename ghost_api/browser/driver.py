"""Control a real Chromium browser with Playwright (FR-7).

Every action ends with `settle()`, which waits for the network to go quiet and then
flushes the traffic recorder so response bodies are read before the next action.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from ghost_api.browser.egress import EgressProxy
from ghost_api.browser.gate import RequestGate
from ghost_api.browser.snapshot import (
    SNAPSHOT_ARGS,
    SNAPSHOT_JS,
    PageSnapshot,
    build_snapshot,
)
from ghost_api.capture.models import CapturedRequest
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.config import tool_user_agent
from ghost_api.net import RobotsPolicy
from ghost_api.netguard import NetworkPolicy

NAV_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 8_000
QUIET_MS = 600
POLL_MS = 100
ACTION_TIMEOUT_MS = 5_000


class ActionError(Exception):
    """An agent action could not be performed; the message is shown to the agent."""


class BrowserSession:
    def __init__(
        self,
        start_url: str,
        recorder: TrafficRecorder,
        headed: bool = False,
        policy: NetworkPolicy | None = None,
        gate: RequestGate | None = None,
        on_block: Callable[[str], None] | None = None,
        storage_state: dict[str, Any] | None = None,
    ) -> None:
        """`policy`: where the browser may connect (default: decided from `start_url`).
        `gate`: robots.txt and rate limits for the browser's own requests."""
        self.start_url = start_url
        self.recorder = recorder
        self.headed = headed
        self.policy = policy or NetworkPolicy.for_target(start_url)
        self.gate = gate
        self.on_block = on_block
        self.storage_state = storage_state  # a saved login (auth_state.py)
        self._proxy: EgressProxy | None = None
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._inflight = 0
        self.popup_urls: list[str] = []

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("BrowserSession is not open")
        return self._page

    @property
    def host(self) -> str:
        return urlsplit(self.start_url).hostname or ""

    def open(self) -> BrowserSession:
        launch: dict[str, Any] = {"headless": not self.headed}
        if not self.policy.allow_private:
            # Every connection goes through the egress proxy, loopback included (Chromium
            # skips the proxy for localhost unless told "<-loopback>").
            self._proxy = EgressProxy(self.policy, self._blocked).start()
            launch["proxy"] = {"server": self._proxy.url, "bypass": "<-loopback>"}
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(**launch)
        # Keep the real browser user-agent (so sites behave normally) and add an honest
        # identifier for this tool (NFR-20).
        probe = self._browser.new_page()
        base_ua = probe.evaluate("navigator.userAgent")
        probe.close()
        self._context = self._browser.new_context(
            user_agent=f"{base_ua} {tool_user_agent()}",
            storage_state=self.storage_state,  # type: ignore[arg-type]
        )
        if self.gate is not None:
            self._context.route("**/*", self.gate.handle)
        self._page = self._context.new_page()
        # Registered after the main tab exists, so only real popups are caught.
        self._context.on("page", self._on_popup)
        self._page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self._page.on("request", lambda _: self._track(+1))
        self._page.on("requestfinished", lambda _: self._track(-1))
        self._page.on("requestfailed", lambda _: self._track(-1))
        self.recorder.attach(self._page)
        return self

    def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):  # best-effort cleanup
                    closer.close()
        if self._pw is not None:
            self._pw.stop()
        if self._proxy is not None:
            self._proxy.stop()
        self._page = self._context = self._browser = self._pw = None
        self._proxy = None

    def export_state(self) -> dict[str, Any]:
        """Cookies and site storage now (after the user logged in), for auth_state.save."""
        if self._context is None:
            raise RuntimeError("BrowserSession is not open")
        return dict(self._context.storage_state())

    def _blocked(self, message: str) -> None:
        self.recorder.dropped["blocked-network"] += 1
        if self.on_block is not None:
            self.on_block(message)

    def __enter__(self) -> BrowserSession:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- actions ---

    def settle(self) -> list[CapturedRequest]:
        """Wait until no requests have been in flight for QUIET_MS, then collect traffic.

        Playwright's "networkidle" load state only applies to the initial page load, so
        requests triggered later (by a scroll or click) are tracked here instead. Sites
        with long-polling never go quiet; SETTLE_TIMEOUT_MS caps the wait.
        """
        waited = quiet = 0
        while waited < SETTLE_TIMEOUT_MS:
            self.page.wait_for_timeout(POLL_MS)  # lets Playwright deliver events
            waited += POLL_MS
            quiet = quiet + POLL_MS if self._inflight == 0 else 0
            if quiet >= QUIET_MS:
                break
        return self.recorder.flush()

    def _track(self, delta: int) -> None:
        self._inflight = max(0, self._inflight + delta)

    def _on_popup(self, popup: Page) -> None:
        """Keep everything in one tab: remember where a popup was going, then close it."""
        if popup is self._page:
            return
        with contextlib.suppress(PlaywrightError):
            popup.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
            self.popup_urls.append(popup.url)
            popup.close()

    @property
    def url(self) -> str:
        return self.page.url

    def snapshot(self, target_host: str, robots: RobotsPolicy | None = None) -> PageSnapshot:
        raw = self.page.evaluate(SNAPSHOT_JS, SNAPSHOT_ARGS)
        return build_snapshot(raw, target_host, robots)

    def screenshot(self, path: Path) -> bool:
        """Save a small JPEG of what the browser shows (for the dashboard's live view)."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), type="jpeg", quality=55)
        except PlaywrightError:
            return False
        return True

    def element_box(self, element_id: int) -> dict[str, float] | None:
        """Where an element is on the page (viewport pixels), before acting on it."""
        try:
            box = self.page.locator(f'[data-ghost-id="{element_id}"]').first.bounding_box(
                timeout=1_000
            )
        except PlaywrightError:
            return None
        if box is None:
            return None
        viewport = self.page.viewport_size or {"width": 1280, "height": 720}
        return {
            "x": box["x"],
            "y": box["y"],
            "width": box["width"],
            "height": box["height"],
            "viewport_width": viewport["width"],
            "viewport_height": viewport["height"],
        }

    def _element(self, element_id: int):  # type: ignore[no-untyped-def]
        loc = self.page.locator(f'[data-ghost-id="{element_id}"]').first
        if loc.count() == 0:
            raise ActionError(f"element [{element_id}] is no longer on the page")
        return loc

    def _after_action(self) -> list[CapturedRequest]:
        # A click may start a navigation; give the new document a moment to load.
        with contextlib.suppress(PlaywrightError):
            self.page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
        return self.settle()

    def click(self, element_id: int) -> list[CapturedRequest]:
        loc = self._element(element_id)
        try:
            loc.click(timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError:
            # Covered by an overlay or animating: fall back to a DOM click.
            try:
                loc.evaluate("el => el.click()")
            except PlaywrightError as exc:
                raise ActionError(f"could not click [{element_id}]: {_first_line(exc)}") from exc
        return self._after_action()

    def type_text(self, element_id: int, text: str, submit: bool) -> list[CapturedRequest]:
        loc = self._element(element_id)
        try:
            loc.fill(text, timeout=ACTION_TIMEOUT_MS)
            if submit:
                loc.press("Enter", timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise ActionError(f"could not type into [{element_id}]: {_first_line(exc)}") from exc
        return self._after_action()

    def go_back(self) -> list[CapturedRequest]:
        try:
            self.page.go_back(wait_until="domcontentloaded")
        except PlaywrightError as exc:
            raise ActionError(f"could not go back: {_first_line(exc)}") from exc
        return self.settle()

    def goto(self, url: str) -> list[CapturedRequest]:
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            raise ActionError(f"could not open {url}: {_first_line(exc)}") from exc
        return self.settle()

    def scroll(self, direction: str = "down") -> list[CapturedRequest]:
        if direction == "bottom":
            self.page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        else:
            delta = -1 if direction == "up" else 1
            viewport = self.page.viewport_size
            height = viewport["height"] if viewport else 800
            self.page.mouse.wheel(0, delta * height)
        return self.settle()


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:160] if str(exc).strip() else type(exc).__name__
