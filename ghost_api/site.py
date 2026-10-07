"""Which site a run belongs to: the key for its memory and its output folder.

Usually that is the host ("hn.algolia.com"). When the start URL begins with a path that
names a section of the site, that first segment is kept too ("www.google.com/maps"), so
one big host's separate apps (Maps, Search, Support...) don't share memory and files.
The host alone still decides what counts as the site's own API versus a third party.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from ghost_api.analysis.url_grouping import looks_like_id

# A section name: a short plain word, no dots (files), no encoded data.
_SECTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
# Locale prefixes (/en/, /en-us/, /pt_BR/) say which language, not which app.
_LOCALE_RE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")
_HOST_RE = r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?"
SITE_RE = re.compile(rf"^{_HOST_RE}(/[A-Za-z][A-Za-z0-9_-]{{0,39}})?$")


def site_key(url: str) -> str:
    """'https://www.google.com/maps/@12.9,77.6' -> 'www.google.com/maps';
    'https://hn.algolia.com/' -> 'hn.algolia.com'."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    if segments and is_section(segments[0]):
        return f"{host}/{segments[0]}"
    return host


def is_section(segment: str) -> bool:
    return bool(
        _SECTION_RE.match(segment) and not _LOCALE_RE.match(segment) and not looks_like_id(segment)
    )


def site_host(site: str) -> str:
    """The host part: 'www.google.com/maps' -> 'www.google.com'."""
    return site.split("/", 1)[0]


def is_site(name: str) -> bool:
    """A safe site name (used in file paths and URLs): host, optionally /section."""
    return bool(SITE_RE.match(name))


# In dashboard URLs a site is one path segment, so its "/" is written as "~".
def to_url_id(site: str) -> str:
    return site.replace("/", "~")


def from_url_id(url_id: str) -> str:
    return url_id.replace("~", "/", 1)


class SiteAmbiguous(ValueError):
    def __init__(self, name: str, choices: list[str]) -> None:
        self.choices = choices
        super().__init__(f"{name} has several sites: {', '.join(choices)}. Name one of them.")


def resolve_site(name: str, known: Iterable[str]) -> str:
    """What the user typed -> a stored site name.

    Accepts a site ('www.google.com/maps'), a URL, or a bare domain. A bare domain with
    exactly one stored site under it means that site; with several, SiteAmbiguous.
    Unknown names are returned as typed (the caller reports "nothing stored")."""
    name = name.strip().rstrip("/")
    if "://" in name:
        name = site_key(name)
    sites = set(known)
    if name in sites:
        return name
    under = sorted(s for s in sites if s.startswith(name + "/"))
    if len(under) == 1:
        return under[0]
    if under:
        raise SiteAmbiguous(name, under)
    return name
