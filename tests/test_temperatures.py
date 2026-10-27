"""Temperature level assignment by fixed sensor position (FR-UI-1b)."""

from typing import Any

import pytest

from orchestrator.models import BoilerStatus, ModelError
from orchestrator.temperatures import TemperatureLevels, assign_levels, levels_of

SAMPLE: dict[str, Any] = {
    "ActiveYesNo": "Yes",
    "CurrentShowers": 3.43,
    "CurrentShowersnew": 4.92,
    "CurrentTemp": [58.06, 54.19, 61.44],
    "Date": "2026-10-24T16:40:19.707843",
    "HeaterOnOff": "Off",
    "MinVal": 4.0,
    "Status": "Manual",
    "TargetVal": 4.0,
    "TimeToStartMin": 175,
}


def test_the_sample_reading_is_placed_by_sensor_position() -> None:
    # the legacy page: Upper = [2], Mid = [0], Lower = [1]
    levels = assign_levels([58.06, 54.19, 61.44])
    assert levels == TemperatureLevels(upper=61.44, mid=58.06, lower=54.19)


def test_a_warm_lower_sensor_stays_in_the_lower_position() -> None:
    # hot day: water entering at the bottom is warmer than the water above it; no sorting
    levels = assign_levels([50.0, 62.0, 55.0])
    assert levels == TemperatureLevels(upper=55.0, mid=50.0, lower=62.0)


def test_equal_readings_are_fine() -> None:
    assert assign_levels([50.0, 50.0, 50.0]) == TemperatureLevels(50.0, 50.0, 50.0)
    assert assign_levels([60.0, 50.0, 60.0]) == TemperatureLevels(60.0, 60.0, 50.0)


def test_the_not_yet_measured_placeholder_is_passed_through() -> None:
    assert assign_levels([-1, -1, -1]) == TemperatureLevels(-1, -1, -1)


@pytest.mark.parametrize("temps", [[], [1.0], [1.0, 2.0], [1.0, 2.0, 3.0, 4.0]])
def test_exactly_three_readings_are_required(temps: list[float]) -> None:
    with pytest.raises(ModelError, match="exactly three"):
        assign_levels(temps)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_readings_are_refused(bad: float) -> None:
    with pytest.raises(ModelError, match="finite"):
        assign_levels([50.0, bad, 60.0])


def test_levels_of_a_status_uses_the_positions_of_the_pis_list() -> None:
    status = BoilerStatus.parse(SAMPLE)
    assert status.temps == (58.06, 54.19, 61.44)  # the Pi's own order
    assert levels_of(status) == TemperatureLevels(upper=61.44, mid=58.06, lower=54.19)


def test_as_dict_uses_the_labels_of_the_ui_and_mqtt() -> None:
    assert assign_levels([58.06, 54.19, 61.44]).as_dict() == {
        "upper": 61.44,
        "mid": 58.06,
        "lower": 54.19,
    }
