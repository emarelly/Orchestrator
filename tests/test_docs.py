"""The documents agree with the code: traceability, README, changelog (CS-21, DoD 3 and 6)."""

import re
import tomllib
from pathlib import Path

from orchestrator import __version__
from orchestrator import config as config_module

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = (ROOT / "docs" / "Requirements.md").read_text(encoding="utf-8")
TRACEABILITY = (ROOT / "docs" / "traceability.md").read_text(encoding="utf-8")
ID = re.compile(r"(?m)^\s*(?:- )?\*\*((?:FR|NFR|CS|LOG|OPS)(?:-[A-Z]+)?-[0-9]+[a-z]?)\b")
TEST_REF = re.compile(r"^tests/(\w+\.py)(?:::(\w+))?$")
PATH_REF = re.compile(r"^(?:tools|deploy|docs|orchestrator|ui-mock)/[\w./-]+\.\w+$")


def requirement_ids() -> list[str]:
    return sorted(set(ID.findall(REQUIREMENTS)))


def mentioned(requirement: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9-]){re.escape(requirement)}(?![A-Za-z0-9])", text) is not None


def test_every_requirement_is_in_the_traceability_list() -> None:
    ids = requirement_ids()
    assert len(ids) > 100  # the pattern really finds the requirements
    missing = [i for i in ids if not mentioned(i, TRACEABILITY)]
    assert missing == [], f"not in docs/traceability.md: {missing}"


def test_every_test_and_file_named_in_the_traceability_list_exists() -> None:
    problems: list[str] = []
    for line in TRACEABILITY.splitlines():
        if not line.startswith("|"):
            continue
        current: str | None = None
        for token in re.findall(r"`([^`]+)`", line):
            if token.startswith("::"):
                name, file = token[2:], current
            else:
                match = TEST_REF.match(token)
                if match:
                    current, name, file = match.group(1), match.group(2), match.group(1)
                elif PATH_REF.match(token) or token == "pyproject.toml":
                    if not (ROOT / token).exists():
                        problems.append(f"missing file {token}")
                    continue
                else:
                    continue
            if file is None or not (ROOT / "tests" / file).exists():
                problems.append(f"missing test file {file} ({token})")
            elif name and not re.search(
                rf"(?m)^\s*(?:async\s+)?def {re.escape(name)}\(",
                (ROOT / "tests" / file).read_text("utf-8"),
            ):
                problems.append(f"no test {file}::{name}")
    assert problems == []


def test_the_readme_names_every_secret_variable_and_every_config_section() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in config_module.SECRET_VARIABLES:
        assert name in readme, name
    assert config_module.ENV_DRY_RUN in readme and "ORCHESTRATOR_CONFIG" in readme
    example = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    for section in example:
        assert f"`[{section}]`" in readme or f"[{section}]" in readme, section


def test_the_readme_explains_the_required_topics() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    for topic in (
        "install on the pi",
        "google authorization",
        "environment variables",
        "troubleshooting",
        "systemctl",
        "healthz",
        "rollback",
        "dry_run",
    ):
        assert topic in readme, topic


def test_every_command_in_the_readme_exists() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for path in re.findall(r"(?:bash|sudo bash) ((?:tools|deploy)/[\w.]+)", readme):
        assert (ROOT / path).exists(), path
    for module in re.findall(r"-m (orchestrator(?:\.\w+)?|tools\.\w+)", readme):
        assert (ROOT / (module.replace(".", "/") + ".py")).exists() or (
            ROOT / module.replace(".", "/") / "__main__.py"
        ).exists(), module


def test_the_changelog_has_the_current_version() -> None:
    changelog = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {__version__} " in changelog or f"## {__version__}\n" in changelog


def test_the_architecture_page_names_every_module_and_every_diagram_is_closed() -> None:
    page = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    modules = [
        path.stem
        for path in (ROOT / "orchestrator").glob("*.py")
        if path.stem not in ("__init__", "__main__")
    ]
    missing = [name for name in modules if name not in page]
    assert missing == [], f"not in docs/architecture.md: {missing}"
    assert page.count("```mermaid") == 8
    assert page.count("```") == 16  # every opened block is closed
