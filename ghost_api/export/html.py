"""report.html: one self-contained page to share a site's results (no server needed).

The page is the React report viewer built from web/ (`npm run build` writes
viewer/report.html with all scripts and styles inlined). This module puts the site's
data into it as JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TEMPLATE = Path(__file__).parent / "viewer" / "report.html"
DATA_TAG = '<script id="ghost-report" type="application/json">__GHOST_REPORT__</script>'


class ViewerMissing(RuntimeError):
    """The report viewer was not built (run `npm run build` in web/)."""


def embed_json(data: Any) -> str:
    """JSON that is safe inside a <script> element: no '</script>', '<!--' or '&' tricks."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def build_report_html(data: dict[str, Any], template: Path = TEMPLATE) -> str:
    if not template.is_file():
        raise ViewerMissing(f"report viewer not found at {template}")
    page = template.read_text(encoding="utf-8")
    if page.count(DATA_TAG) != 1:
        raise ViewerMissing("report viewer has no data placeholder; rebuild it")
    tag = DATA_TAG.replace("__GHOST_REPORT__", embed_json(data))
    return page.replace(DATA_TAG, tag)
