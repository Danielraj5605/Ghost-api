"""The interactive session in a real Windows pseudo-console (ConPTY).

Unit tests feed keys straight to prompt_toolkit, so they missed a real bug: the Ctrl+C
handling turned off Windows' own line editing and Backspace/Enter stopped working at the
goal question. This test types into a real console, like a person would.
"""

import os
import re
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

winpty = pytest.importorskip("winpty", reason="needs pywinpty (Windows, dev extra)")
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows console")

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Za-z]|\r")
BACKSPACE = "\x7f"
CTRL_C = "\x03"


class Console:
    def __init__(self, cwd: Path) -> None:
        env = dict(os.environ, GHOST_API_DB=str(cwd / "ghost_api.db"), PYTHONIOENCODING="utf-8")
        self.proc = winpty.PtyProcess.spawn(
            [sys.executable, "-m", "ghost_api"], cwd=str(cwd), env=env, dimensions=(45, 140)
        )
        self.chunks: list[str] = []
        self.pos = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                self.chunks.append(self.proc.read(4096))
            except EOFError:
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.05)

    def text(self) -> str:
        return ANSI.sub("", "".join(self.chunks))

    def expect(self, needle: str, timeout: float = 45) -> None:
        end = time.time() + timeout
        while time.time() < end:
            i = self.text().find(needle, self.pos)
            if i >= 0:
                self.pos = i + len(needle)
                return
            time.sleep(0.1)
        raise AssertionError(f"never saw {needle!r}; screen ends with:\n{self.text()[-800:]}")

    def type(self, keys: str) -> None:
        for ch in keys:
            self.proc.write(ch)
            time.sleep(0.06)  # slower than a person types, so a busy machine keeps up


@pytest.fixture
def console(tmp_path: Path) -> Iterator[Console]:
    c = Console(tmp_path)
    yield c
    if c.proc.isalive():
        c.proc.terminate(force=True)


def test_typing_editing_and_exiting_in_a_real_console(console: Console) -> None:
    console.expect("Welcome to Ghost API", 60)
    console.type("/hepl" + BACKSPACE * 2 + "lp\r")  # Backspace fixes the typo
    console.expect("Commands")
    console.type("https://news.example.com/home\r")  # a URL alone: the goal is asked
    console.expect("What do you want from this site?")
    console.type("lis" + BACKSPACE * 2)  # editing works in the question too
    time.sleep(0.3)
    console.type(CTRL_C)  # cancels the question, not the session
    console.expect("nothing started")
    console.type(CTRL_C)
    console.expect("Press Ctrl+C again to exit")
    console.type(CTRL_C)
    console.expect("Goodbye")
    time.sleep(1)
    assert not console.proc.isalive()


def test_ctrl_c_during_a_run_stops_only_that_run(console: Console) -> None:
    from evals.demo_shop import serve as serve_shop

    shop = serve_shop(0)
    try:
        console.expect("Welcome to Ghost API", 60)
        console.type("/agent off\r")
        console.expect("only load and scroll")
        console.type(f"http://127.0.0.1:{shop.server_address[1]}/ list prodcts"
                     + BACKSPACE * 3 + "ucts\r")  # fmt: skip
        console.expect("goal list products", 60)
        time.sleep(1.5)  # the browser is starting: interrupting it here used to hang
        console.type(CTRL_C)
        console.expect("Stopping after the current step")
        console.expect("interrupted", 120)
        console.expect("Next:", 60)
        console.type("/exit\r")
        console.expect("Goodbye")
    finally:
        shop.shutdown()
