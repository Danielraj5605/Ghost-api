"""A compact, LLM-friendly view of the current page (FR-8, NFR-6).

JavaScript in the page lists visible interactive elements and tags each with a
`data-ghost-id` attribute so the agent can refer to it by number. Python then removes
elements the agent must never use (FR-10, FR-11, NFR-21) and caps the list size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ghost_api.capture.filters import is_same_site
from ghost_api.net import RobotsPolicy

MAX_ELEMENTS = 60
MAX_LABEL = 60
MAX_TEXT = 400

# Labels or URLs of actions that change server state, spend money or end the session.
DESTRUCTIVE_RE = re.compile(
    r"\b(delete|remove|destroy|erase|buy|purchase|checkout|check out|pay|payment|place order|"
    r"order now|add to (cart|bag|basket)|subscribe|unsubscribe|log ?out|sign ?out|logout|"
    r"signout|deactivate|close account|cancel (my )?(account|subscription|order)|donate|"
    r"report|block|unfollow|follow|upvote|downvote|vote|flag|hide|reply|comment|send|share|"
    r"invite|transfer|withdraw|confirm)\b",
    re.I,
)
# Input types the agent must not fill in.
FORBIDDEN_INPUT_TYPES = {"password", "file", "hidden"}

SNAPSHOT_JS = r"""
({ MAX_LABEL, MAX_TEXT }) => {
  document.querySelectorAll('[data-ghost-id]').forEach(e => e.removeAttribute('data-ghost-id'));
  const selector = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]', '[role=option]',
    '[role=checkbox]', '[role=switch]', '[role=searchbox]', '[role=combobox]', '[onclick]',
    '[contenteditable=true]'
  ].join(',');
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity) > 0.05;
  };
  const labelOf = el => {
    const parts = [
      el.getAttribute('aria-label'), el.innerText, el.getAttribute('placeholder'),
      el.getAttribute('title'), el.getAttribute('alt'), el.getAttribute('name'),
      el.tagName === 'INPUT' && ['submit', 'button'].includes(el.type) ? el.value : '',
      el.querySelector && el.querySelector('img[alt]') ? el.querySelector('img[alt]').alt : ''
    ];
    for (const p of parts) { const c = clean(p); if (c) return c.slice(0, MAX_LABEL); }
    return '';
  };
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (!visible(el) || el.disabled) continue;
    // Open links in this tab so the recorder sees their traffic.
    if (el.tagName === 'A' && el.target) el.target = '_self';
    const r = el.getBoundingClientRect();
    n += 1;
    el.setAttribute('data-ghost-id', String(n));
    out.push({
      id: n,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      type: (el.getAttribute('type') || '').toLowerCase(),
      label: labelOf(el),
      href: el.tagName === 'A' ? el.href : '',
      download: el.hasAttribute('download'),
      inViewport: r.bottom > 0 && r.top < window.innerHeight,
      top: r.top + window.scrollY
    });
  }
  const doc = document.documentElement;
  const maxScroll = doc.scrollHeight - window.innerHeight;
  const scroll = maxScroll <= 5 ? 'no-scroll'
    : window.scrollY <= 5 ? 'top'
    : window.scrollY >= maxScroll - 5 ? 'bottom' : 'middle';
  const main = document.querySelector('main, [role=main], #content, article') || document.body;
  return {
    url: location.href,
    title: document.title,
    scroll,
    text: clean(main ? main.innerText : '').slice(0, MAX_TEXT),
    elements: out
  };
}
"""
SNAPSHOT_ARGS = {"MAX_LABEL": MAX_LABEL, "MAX_TEXT": MAX_TEXT}


@dataclass
class Element:
    id: int
    kind: str  # link, button, input:text, select, ...
    label: str
    href: str = ""
    in_viewport: bool = False

    def describe(self) -> str:
        text = (
            f'[{self.id}] {self.kind} "{self.label}"' if self.label else f"[{self.id}] {self.kind}"
        )
        if self.href:
            text += f" -> {_short_href(self.href)}"
        return text


@dataclass
class PageSnapshot:
    url: str
    title: str
    scroll: str
    text: str
    elements: list[Element] = field(default_factory=list)
    blocked: int = 0  # elements removed for safety
    hidden: int = 0  # elements left out to keep the prompt small

    def element(self, element_id: int) -> Element | None:
        return next((e for e in self.elements if e.id == element_id), None)


def kind_of(raw: dict[str, Any]) -> str:
    tag = str(raw.get("tag") or "")
    role = str(raw.get("role") or "")
    typ = str(raw.get("type") or "")
    if role:
        return role
    if tag == "a":
        return "link"
    if tag == "input":
        return f"input:{typ or 'text'}"
    if tag in ("textarea", "select", "summary", "button"):
        return tag
    return "clickable"


def block_reason(
    raw: dict[str, Any], target_host: str, robots: RobotsPolicy | None = None
) -> str | None:
    """Why the agent must not use this element, or None if it's allowed."""
    label = str(raw.get("label") or "")
    href = str(raw.get("href") or "")
    kind = kind_of(raw)
    if raw.get("download"):
        return "download"
    if kind.startswith("input:") and kind.split(":", 1)[1] in FORBIDDEN_INPUT_TYPES:
        return "sensitive input"
    if DESTRUCTIVE_RE.search(label) or (href and DESTRUCTIVE_RE.search(urlsplit(href).path)):
        return "destructive"
    if href:
        scheme = urlsplit(href).scheme
        if scheme in ("mailto", "tel", "sms", "data", "blob"):
            return f"{scheme} link"
        if scheme in ("http", "https"):
            if not is_same_site(href, target_host):
                return "off-site"
            if robots is not None and not robots.allows(href):
                return "robots.txt"
    return None


def build_snapshot(
    raw: dict[str, Any], target_host: str, robots: RobotsPolicy | None = None
) -> PageSnapshot:
    """Turn the page script's output into a safe, size-capped snapshot."""
    kept: list[Element] = []
    seen: set[tuple[str, str, str]] = set()
    blocked = 0
    for item in raw.get("elements", []):
        if block_reason(item, target_host, robots):
            blocked += 1
            continue
        el = Element(
            id=int(item["id"]),
            kind=kind_of(item),
            label=str(item.get("label") or ""),
            href=str(item.get("href") or ""),
            in_viewport=bool(item.get("inViewport")),
        )
        key = (el.kind, el.label, el.href)
        if key in seen:  # e.g. twenty identical "Read more" links
            continue
        seen.add(key)
        kept.append(el)
    # Elements on screen first; the rest in page order.
    kept.sort(key=lambda e: not e.in_viewport)
    shown = kept[:MAX_ELEMENTS]
    return PageSnapshot(
        url=str(raw.get("url", "")),
        title=str(raw.get("title", ""))[:120],
        scroll=str(raw.get("scroll", "")),
        text=str(raw.get("text", "")),
        elements=shown,
        blocked=blocked,
        hidden=len(kept) - len(shown),
    )


def _short_href(href: str) -> str:
    parts = urlsplit(href)
    if parts.scheme == "javascript":
        return "(script)"
    path = parts.path or "/"
    text = (
        path
        + (f"?{parts.query}" if parts.query else "")
        + (f"#{parts.fragment}" if parts.fragment else "")
    )
    return text if len(text) <= 70 else text[:67] + "..."
