"""The check tool itself (CS-4)."""

import pytest

from tools import check


def test_steps_cover_format_lint_types_and_tests() -> None:
    steps = check.build_steps("py")
    assert [step.name for step in steps] == ["black", "ruff", "mypy", "pytest"]
    assert all(step.args[0] == "py" for step in steps)


def test_main_returns_zero_when_all_steps_pass() -> None:
    assert check.main([], runner=lambda _step: True) == 0


def test_main_runs_every_step_and_fails_if_one_fails(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[str] = []

    def runner(step: check.Step) -> bool:
        seen.append(step.name)
        return step.name != "ruff"

    assert check.main([], runner=runner) == 1
    assert seen == ["black", "ruff", "mypy", "pytest"]
    assert "CHECK FAILED: ruff" in capsys.readouterr().out


def test_main_rejects_arguments() -> None:
    assert check.main(["--fast"], runner=lambda _step: True) == 2
