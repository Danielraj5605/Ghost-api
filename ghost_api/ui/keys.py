"""Ctrl+C during a run inside the interactive session, read as a key, not a signal.

On Windows a Ctrl+C *signal* also reaches cmd.exe, which runs the `ghost-api.cmd`
launcher; when Ghost API later exits, cmd asks "Terminate batch job (Y/N)?". While a run
is going (and only then: never while you type), `ctrl_c_as_key()` switches the console so
Ctrl+C arrives as an ordinary key, read by a small thread. The first press asks the run
to stop at the next safe point (`stop_requested()`, checked between steps); a second
press interrupts at once; a third quits. The session itself keeps going.

That console switch also turns off Windows' line editing (Backspace, Enter), so anything
that reads a typed answer during a run must use `input_paused()`, which puts the normal
mode back while the user types.

On other systems, and when input isn't a console, these do nothing.
"""

from __future__ import annotations

import _thread
import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator

ENABLE_PROCESSED_INPUT = 0x0001
STD_INPUT_HANDLE = -10


class _Watcher:
    def __init__(self, set_mode: Callable[[bool], None]) -> None:
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.presses = 0
        self.set_mode = set_mode  # True: normal console mode (typing works)

    def run(self) -> None:
        import msvcrt

        while not self.stop.is_set():
            if self.pause.is_set() or not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            if self.pause.is_set():  # paused while waiting: leave the key to the reader
                continue
            if msvcrt.getwch() == "\x03":
                self.presses += 1
                pressed(self.presses)


_active: _Watcher | None = None
_stop = threading.Event()


def _say(message: str) -> None:
    pass


# Set by the session: how to tell the user that a stop is on its way.
on_stop_requested: Callable[[str], None] = _say


def stop_requested() -> bool:
    """Has the user asked the current run to stop? (Checked between steps.)"""
    return _stop.is_set()


def pressed(presses: int) -> None:
    """1st Ctrl+C: stop at the next safe point (a browser that is starting or loading a
    page must not be interrupted half-way: it can hang). 2nd: interrupt now. 3rd: quit."""
    if presses == 1:
        _stop.set()
        on_stop_requested("Stopping after the current step... (Ctrl+C again to stop at once)")
    elif presses == 2:
        _thread.interrupt_main()
    else:
        os._exit(130)


@contextlib.contextmanager
def ctrl_c_as_key() -> Iterator[None]:
    """Use around running something (not around reading input)."""
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
    normal = mode.value

    def set_mode(typing: bool) -> None:
        kernel.SetConsoleMode(handle, normal if typing else normal & ~ENABLE_PROCESSED_INPUT)

    watcher = _Watcher(set_mode)
    _stop.clear()
    set_mode(False)
    thread = threading.Thread(target=watcher.run, daemon=True)
    _active = watcher
    thread.start()
    try:
        yield
    finally:
        watcher.stop.set()
        thread.join(timeout=1)
        set_mode(True)
        _active = None
        _stop.clear()


@contextlib.contextmanager
def input_paused() -> Iterator[None]:
    """Let the user type an answer during a run: the watcher stops reading keys and the
    console's normal mode (Backspace, Enter, Ctrl+C) comes back until the answer is in."""
    watcher = _active
    if watcher is None:
        yield
        return
    watcher.pause.set()
    time.sleep(0.06)  # let the watcher finish any key it was reading
    watcher.set_mode(True)
    try:
        yield
    finally:
        watcher.set_mode(False)
        watcher.pause.clear()
