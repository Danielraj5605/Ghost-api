"""Control a real Chromium browser with Playwright (FR-7).

Every action ends with `settle()`, which waits for the network to go quiet and then
flushes the traffic recorder so response bodies are read before the next action.
"""

from __future__ import annotations

import contextlib
from types import TracebackType
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from ghost_api.capture.models import CapturedRequest
from ghost_api.capture.recorder import TrafficRecorder
from ghost_api.config import USER_AGENT_SUFFIX

NAV_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 8_000
QUIET_MS = 600
POLL_MS = 100


class BrowserSession:
    def __init__(self, start_url: str, recorder: TrafficRecorder, headed: bool = False) -> None:
        self.start_url = start_url
        self.recorder = recorder
        self.headed = headed
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._inflight = 0

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("BrowserSession is not open")
        return self._page

    @property
    def host(self) -> str:
        return urlsplit(self.start_url).hostname or ""

    def open(self) -> BrowserSession:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not self.headed)
        # Keep the real browser user-agent (so sites behave normally) and add an honest
        # identifier for this tool (NFR-20).
        probe = self._browser.new_page()
        base_ua = probe.evaluate("navigator.userAgent")
        probe.close()
        self._context = self._browser.new_context(user_agent=f"{base_ua} {USER_AGENT_SUFFIX}")
        self._page = self._context.new_page()
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
        self._page = self._context = self._browser = self._pw = None

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

    def goto(self, url: str) -> list[CapturedRequest]:
        self.page.goto(url, wait_until="domcontentloaded")
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
