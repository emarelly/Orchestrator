"""Package basics (CS-21: the version is a single constant)."""

import re
import tomllib
from pathlib import Path

import orchestrator

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_semantic() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", orchestrator.__version__)


def test_version_matches_pyproject() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["version"] == orchestrator.__version__
