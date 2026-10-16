"""Saved logins: the browser session (cookies and site storage) after you log in yourself.

`ghost-api login URL` opens a visible browser; you log in by hand (Ghost API never sees or
stores a password); the session is then saved per site. Later runs and refreshes open the
browser already logged in, and fetch / watch / serve add the session's cookies to their
direct calls. `ghost-api logout SITE` (or `forget`) deletes it.

At rest:
  - Windows: encrypted with DPAPI for your Windows account, so another account, or a copy
    of the file on another machine, can't read it.
  - Elsewhere: a file only your user can read (mode 600), not encrypted. Use disk
    encryption, or log out when you are done.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ghost_api.site import to_url_id

MAGIC_DPAPI = b"GHOSTAPI-DPAPI1\n"
MAGIC_PLAIN = b"GHOSTAPI-PLAIN1\n"


class LoginError(Exception):
    """A saved login can't be read (damaged, or made by another Windows account)."""


def logins_dir(db_path: Path) -> Path:
    """Next to the database, like screenshots: never in the output folder."""
    return db_path.parent / f"{db_path.stem}_files" / "logins"


def _file(db_path: Path, site: str) -> Path:
    return logins_dir(db_path) / f"{to_url_id(site)}.session"


def save(db_path: Path, site: str, state: dict[str, Any]) -> dict[str, Any]:
    """Save a Playwright storage state; returns what was saved (no values)."""
    meta = {
        "site": site,
        "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cookies": len(state.get("cookies", [])),
        "domains": sorted({str(c.get("domain", "")).lstrip(".") for c in state.get("cookies", [])}),
        "encrypted": sys.platform == "win32",
    }
    payload = json.dumps({"meta": meta, "state": state}).encode("utf-8")
    path = _file(db_path, site)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = MAGIC_DPAPI + _protect(payload) if meta["encrypted"] else MAGIC_PLAIN + payload
    # Created with owner-only permissions before any secret is written.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return meta


def load(db_path: Path, site: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(state, meta) for a site's saved login, or None when there is none."""
    path = _file(db_path, site)
    if not path.exists():
        return None
    data = path.read_bytes()
    try:
        if data.startswith(MAGIC_DPAPI):
            payload = _unprotect(data[len(MAGIC_DPAPI) :])
        elif data.startswith(MAGIC_PLAIN):
            payload = data[len(MAGIC_PLAIN) :]
        else:
            raise LoginError("unknown format")
        saved = json.loads(payload)
        return saved["state"], saved["meta"]
    except (OSError, ValueError, KeyError) as exc:
        raise LoginError(
            f"The saved login for {site} can't be read ({exc}). Run `ghost-api login` again."
        ) from exc


def delete(db_path: Path, site: str) -> bool:
    path = _file(db_path, site)
    if path.exists():
        path.unlink()
        return True
    return False


def saved_sites(db_path: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(logins_dir(db_path).glob("*.session")):
        try:
            data = path.read_bytes()
            if data.startswith(MAGIC_DPAPI):
                data = MAGIC_PLAIN + _unprotect(data[len(MAGIC_DPAPI) :])
            out.append(json.loads(data[len(MAGIC_PLAIN) :])["meta"])
        except (OSError, ValueError, KeyError):
            out.append({"site": path.stem, "saved_at": "?", "damaged": True})
    return out


def cookie_header(state: dict[str, Any], url: str, now: float | None = None) -> str:
    """The Cookie header a browser would send to `url` with this session."""
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path or "/"
    now = time.time() if now is None else now
    pairs = []
    for cookie in state.get("cookies", []):
        domain = str(cookie.get("domain", "")).lower()
        bare = domain.lstrip(".")
        if not (host == bare or host.endswith("." + bare)):
            continue
        if not path.startswith(str(cookie.get("path") or "/")):
            continue
        if cookie.get("secure") and parts.scheme != "https":
            continue
        expires = cookie.get("expires", -1)
        if isinstance(expires, int | float) and 0 < expires < now:
            continue
        pairs.append(f"{cookie['name']}={cookie['value']}")
    return "; ".join(pairs)


def age_days(meta: dict[str, Any]) -> float:
    try:
        saved = datetime.fromisoformat(str(meta["saved_at"]))
    except (KeyError, ValueError):
        return 0.0
    return (datetime.now(UTC) - saved).total_seconds() / 86400


# --- Windows DPAPI (crypt32), through ctypes: no extra dependency ---------------------------


def _protect(data: bytes) -> bytes:
    return _dpapi(data, protect=True)


def _unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise LoginError("this login was saved on Windows and can only be read there")
    return _dpapi(data, protect=False)


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":  # also tells the type checker windll exists below
        raise LoginError("Windows data protection is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1: never show a prompt.
    ok = call(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise LoginError(f"Windows could not {'protect' if protect else 'unlock'} the login")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
