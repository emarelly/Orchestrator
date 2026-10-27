"""Write ``requirements.tested.txt``: the exact versions of the libraries the tests ran with.

``requirements.txt`` names the libraries; ``requirements.lock`` (made on the Pi by
``tools/check_pi_install.sh``, CS-1a / CS-17) is what is deployed. This file is the third list: the
versions in the virtual environment of the developer machine, runtime libraries and everything they
need, without the development tools. It is what to compare the Pi's lock with, and a fallback to
read when a library update breaks something.

Run:  python -m tools.freeze_tested
"""

import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requested(path: Path) -> list[str]:
    """The library names listed in a requirements file (comments and options ignored)."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        match = NAME.match(text)
        if match and not text.startswith("-"):
            names.append(canonical(match.group(0)))
    return names


def closure(roots: list[str]) -> dict[str, str]:
    """{name: version} of the roots and, recursively, everything they require when installed."""
    found: dict[str, str] = {}
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in found:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            raise SystemExit(f"{name} is not installed in this environment") from None
        found[name] = dist.version
        for requirement in dist.requires or []:
            if "extra ==" in requirement:
                continue  # optional extras are not part of what is used
            match = NAME.match(requirement)
            if match and _applies(requirement):
                pending.append(canonical(match.group(0)))
    return found


def _applies(requirement: str) -> bool:
    """True unless an environment marker says the requirement is not for this platform."""
    if ";" not in requirement:
        return True
    marker = requirement.split(";", 1)[1]
    try:
        from packaging.markers import Marker

        return Marker(marker).evaluate()
    except ImportError:
        return "platform_system" not in marker and "sys_platform" not in marker


def render(libraries: dict[str, str]) -> str:
    header = (
        "# Versions of the runtime libraries that the tests ran with (made by python -m "
        "tools.freeze_tested).\n"
        "# NOT the deployment lock: requirements.lock is made on the Pi by\n"
        "# tools/check_pi_install.sh and takes precedence in deploy/install.sh.\n"
        "# Compare the two after each Pi install check.\n"
    )
    lines = [f"{name}=={version}" for name, version in sorted(libraries.items())]
    return header + "\n".join(lines) + "\n"


def main() -> int:
    libraries = closure(requested(ROOT / "requirements.txt"))
    (ROOT / "requirements.tested.txt").write_text(render(libraries), encoding="utf-8", newline="\n")
    print(f"wrote requirements.tested.txt with {len(libraries)} libraries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
