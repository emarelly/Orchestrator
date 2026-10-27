"""The single project check (CS-4): formatting, lint, types and tests.

Run as ``python -m tools.check`` from the project folder. The exit code is 0 only if every step
passes; all steps always run so one run shows every problem. Works the same on Windows and the Pi.
"""

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("orchestrator", "tools", "tests")


@dataclass(frozen=True, slots=True)
class Step:
    """One command of the check."""

    name: str
    args: tuple[str, ...]


def build_steps(python: str = sys.executable) -> list[Step]:
    """Return the check steps, each run with the given Python interpreter."""
    return [
        Step("black", (python, "-m", "black", "--check", "--quiet", *TARGETS)),
        Step("ruff", (python, "-m", "ruff", "check", *TARGETS)),
        Step("mypy", (python, "-m", "mypy")),
        Step("pytest", (python, "-m", "pytest", "-q")),
    ]


def run_step(step: Step) -> bool:
    """Run one step in the project folder and report whether it passed."""
    started = time.monotonic()
    result = subprocess.run(step.args, cwd=ROOT, check=False)
    passed = result.returncode == 0
    elapsed = time.monotonic() - started
    print(f"[{'ok' if passed else 'FAILED'}] {step.name} ({elapsed:.1f} s)", flush=True)
    return passed


def main(
    argv: Sequence[str] | None = None,
    runner: Callable[[Step], bool] = run_step,
) -> int:
    """Run every step; return 0 if all passed, otherwise 1."""
    if argv:
        print("python -m tools.check takes no arguments", file=sys.stderr)
        return 2
    results = [(step.name, runner(step)) for step in build_steps()]
    failed = [name for name, passed in results if not passed]
    if failed:
        print(f"CHECK FAILED: {', '.join(failed)}")
        return 1
    print("CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
