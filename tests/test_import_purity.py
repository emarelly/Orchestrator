"""Importing the program does nothing (CS-7): no connection, thread, file or argument parsing."""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

IMPORT_EVERYTHING = """
import importlib, pkgutil, socket, sys, threading

def refuse(*args, **kwargs):
    raise AssertionError("a network connection was opened at import time")

def refuse_thread(self):
    raise AssertionError("a thread was started at import time")

socket.socket.connect = refuse
socket.create_connection = refuse
threading.Thread.start = refuse_thread
sys.argv = ["unrelated", "--not-an-option-of-ours"]   # legacy modules parsed arguments at import

import orchestrator
names = [m.name for m in pkgutil.walk_packages(orchestrator.__path__, "orchestrator.")]
for name in names:
    if name.endswith("__main__"):
        continue                                       # this one is the program itself
    importlib.import_module(name)
print(len(names), threading.active_count())
"""


def test_importing_any_module_starts_nothing_and_connects_nowhere(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", IMPORT_EVERYTHING],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    count, threads = done.stdout.split()
    assert int(count) > 30 and threads == "1"  # many modules, only the main thread
    assert list(tmp_path.iterdir()) == []  # no file or folder was created in the working folder


def test_nothing_imports_from_the_reference_files() -> None:
    legacy = (
        "BoilerCalander|TimerCalander|BoilerControler|ReadGmailCalendar|GmailMessages|"
        "remoteSSH|BoilerStatus|Astro|Config"
    )
    pattern = re.compile(rf"(?m)^\s*(?:import|from)\s+(?:{legacy})\b")
    for path in list((ROOT / "orchestrator").rglob("*.py")) + list((ROOT / "tools").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), path.name
        assert "ref files" not in text.replace("`ref files`", ""), path.name
