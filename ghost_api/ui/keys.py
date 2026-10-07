"""Ctrl+C during a run inside the interactive session, read as a key, not a signal.

On Windows a Ctrl+C *signal* also reaches cmd.exe, which runs the `ghost-api.cmd`
launcher; when Ghost API later exits, cmd asks "Terminate batch job (Y/N)?". While a run
is going, `ctrl_c_as_key()` switches the console so Ctrl+C arrives as an ordinary key,
and a small thread turns it into a KeyboardInterrupt in the main thread: the run stops
the same way as before, and the session keeps going. `input_paused()` lets a question to
the user (the agent asking for a login) read the keyboard normally.

On other systems, and when input isn't a console, these do nothing.
"""

from __future__ import annotations

import _thread
import contextlib
import sys
import threading
import time
from collections.abc import Iterator

ENABLE_PROCESSED_INPUT = 0x0001
STD_INPUT_HANDLE = -10


class _Watcher:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.presses = 0

    def run(self) -> None:
        import msvcrt

        while not self.stop.is_set():
            if self.pause.is_set() or not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            if msvcrt.getwch() == "\x03":
                self.presses += 1
                _thread.interrupt_main()


_active: _Watcher | None = None


@contextlib.contextmanager
def ctrl_c_as_key() -> Iterator[None]:
    global _active
    if sys.platform != "win32" or not sys.stdin.isatty() or _active is not None:
        yield
        return
    import ctypes

    kernel = ctypes.windll.kernel32
    handle = kernel.GetStdHandle(STD_INPUT_HANDLE)
    mode = ctypes.c_uint32()
    if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
        yield
        return
    kernel.SetConsoleMode(handle, mode.value & ~ENABLE_PROCESSED_INPUT)
    watcher = _Watcher()
    thread = threading.Thread(target=watcher.run, daemon=True)
    _active = watcher
    thread.start()
    try:
        yield
    finally:
        watcher.stop.set()
        thread.join(timeout=1)
        kernel.SetConsoleMode(handle, mode.value)
        _active = None


@contextlib.contextmanager
def input_paused() -> Iterator[None]:
    """Let the user type an answer while a run is going (the watcher stops reading keys)."""
    watcher = _active
    if watcher is None:
        yield
        return
    watcher.pause.set()
    try:
        yield
    finally:
        watcher.pause.clear()
