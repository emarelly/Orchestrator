"""The list of libraries is short and the tested versions are written down (CS-17)."""

from pathlib import Path

from tools.freeze_tested import canonical, closure, render, requested

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {
    "flask",
    "waitress",
    "requests",
    "paho-mqtt",
    "google-auth",
    "google-auth-oauthlib",
    "google-api-python-client",
    "astral",
    "tzdata",
    "paramiko",
    "sdnotify",
}
DEVELOPMENT_TOOLS = {"pytest", "black", "ruff", "mypy", "pytest-cov", "coverage"}


def test_requirements_lists_exactly_the_expected_libraries() -> None:
    names = requested(ROOT / "requirements.txt")
    assert set(names) == EXPECTED and len(names) == len(EXPECTED)
    assert "geopy" not in names  # only for the one-time location setup, never at run time


def test_the_mqtt_library_stays_below_version_2() -> None:
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert any(line.startswith("paho-mqtt<2") for line in text.splitlines())


def test_the_tested_versions_are_pinned_and_hold_no_development_tools() -> None:
    lines = [
        line
        for line in (ROOT / "requirements.tested.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    pinned = {canonical(line.split("==")[0]): line.split("==")[1] for line in lines}
    assert len(pinned) == len(lines) and all("==" in line for line in lines)
    assert set(pinned) >= EXPECTED
    assert not (DEVELOPMENT_TOOLS & set(pinned))
    assert pinned["paho-mqtt"].startswith("1.")


def test_the_rendering_is_sorted_and_says_it_is_not_the_deployment_lock() -> None:
    text = render({"b": "2", "a": "1"})
    assert text.index("a==1") < text.index("b==2")
    assert "NOT the deployment lock" in text


def test_the_closure_follows_requirements_of_requirements() -> None:
    found = closure(["flask"])
    assert {"flask", "werkzeug", "jinja2"} <= set(found)
