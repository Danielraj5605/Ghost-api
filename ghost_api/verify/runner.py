"""Run a generated test file in a subprocess and read the results (FR-32)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Outcome = Literal["passed", "failed", "skipped"]
TIMEOUT_S = 600


@dataclass
class TestOutcome:
    __test__ = False  # not a pytest test class, despite the name

    name: str  # e.g. test_iter_get_quotes
    method: str  # the client method it exercises, e.g. get_quotes
    outcome: Outcome
    message: str = ""  # failure text (traceback tail) or skip reason


def method_of(test_name: str) -> str:
    name = test_name.removeprefix("test_")
    return name.removeprefix("iter_")


def run_tests(test_path: Path, env: dict[str, str] | None = None) -> list[TestOutcome]:
    """Run one generated test file. `env` adds variables (e.g. credentials) for this
    run only; they are passed to the subprocess and never written to disk."""
    test_path = test_path.resolve()  # the subprocess runs in another directory
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        cmd = [
            sys.executable, "-m", "pytest", str(test_path.name),
            "-q", "-p", "no:cacheprovider", f"--junitxml={report}",
            f"--rootdir={test_path.parent}", "-o", "addopts=",
        ]  # fmt: skip
        full_env = {**os.environ, **(env or {})}
        try:
            proc = subprocess.run(
                cmd,
                cwd=test_path.parent,
                env=full_env,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return [TestOutcome("(all)", "", "failed", f"tests timed out after {TIMEOUT_S}s")]
        if not report.exists():
            tail = (proc.stdout + proc.stderr).strip()[-800:]
            return [TestOutcome("(all)", "", "failed", f"pytest did not run: {tail}")]
        return parse_junit(report.read_text(encoding="utf-8"))


def run_node_tests(test_path: Path, env: dict[str, str] | None = None) -> list[TestOutcome]:
    """Run a generated TypeScript test file with Node's built-in runner (Node 23.6+ runs
    .ts files directly). Results come back in the same JUnit format as pytest's."""
    test_path = test_path.resolve()
    node = shutil.which("node")
    if node is None:
        return [TestOutcome("(all)", "", "skipped", "Node.js is not installed")]
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        cmd = [node, "--test", "--test-reporter=junit", f"--test-reporter-destination={report}",
               test_path.name]  # fmt: skip
        try:
            proc = subprocess.run(
                cmd,
                cwd=test_path.parent,
                env={**os.environ, **(env or {})},
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return [TestOutcome("(all)", "", "failed", f"tests timed out after {TIMEOUT_S}s")]
        if not report.exists() or not report.read_text(encoding="utf-8").strip():
            tail = (proc.stdout + proc.stderr).strip()[-800:]
            return [TestOutcome("(all)", "", "failed", f"node did not run the tests: {tail}")]
        return parse_junit(report.read_text(encoding="utf-8"))


def parse_junit(xml_text: str) -> list[TestOutcome]:
    outcomes: list[TestOutcome] = []
    root = ET.fromstring(xml_text)
    for case in root.iter("testcase"):
        name = case.get("name", "")
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        skipped = case.find("skipped")
        if problem is not None:
            text = f"{problem.get('message', '')}\n{problem.text or ''}"
            outcomes.append(TestOutcome(name, method_of(name), "failed", text.strip()))
        elif skipped is not None:
            outcomes.append(
                TestOutcome(name, method_of(name), "skipped", skipped.get("message", ""))
            )
        else:
            outcomes.append(TestOutcome(name, method_of(name), "passed"))
    return outcomes
