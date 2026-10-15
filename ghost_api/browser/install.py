"""Is the browser Ghost API drives installed, and install it if not."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def chromium_path() -> Path | None:
    """Where Playwright's Chromium is, or None when it isn't installed."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            path = Path(pw.chromium.executable_path)
    except PlaywrightError:
        return None
    return path if path.exists() else None


def install_chromium() -> bool:
    """Download Chromium for Playwright (about 150 MB, once). True when it worked."""
    proc = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"], check=False, timeout=1800
    )
    return proc.returncode == 0 and chromium_path() is not None
