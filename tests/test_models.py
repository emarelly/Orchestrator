"""Domain models (CS-3): slots, schedules, manual files, boiler status."""

import dataclasses
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest

from orchestrator.models import (
    BoilerSchedule,
    BoilerSlot,
    BoilerStatus,
    ManualEntry,
    ManualOverride,
    ModelError,
    StatusParseError,
    TimerEntry,
    TimerSchedule,
    format_number,
    format_time,
    parse_time,
    to_decimal,
)

SAMPLE_STATUS: dict[str, Any] = {
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


# --- time and number helpers ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("06:00:00", time(6, 0, 0)), ("17:34:50", time(17, 34, 50)), ("12:30", time(12, 30, 0))],
)
def test_parse_time(text: str, expected: time) -> None:
    assert parse_time(text) == expected


@pytest.mark.parametrize("text", ["", "6:00:00", "24:00:00", "12:61:00", "12:00:00:00", "noon"])
def test_parse_time_rejects_bad_text(text: str) -> None:
    with pytest.raises(ModelError):
        parse_time(text)


def test_format_time_is_zero_padded_hh_mm_ss() -> None:
    assert format_time(time(6, 5, 9)) == "06:05:09"


@pytest.mark.parametrize("text", ["3", "3.0", "3.60", "0", "4.25"])
def test_numbers_keep_the_way_they_were_written(text: str) -> None:
    assert format_number(to_decimal(text)) == text


def test_floats_are_converted_through_their_shortest_text() -> None:
    assert format_number(to_decimal(3.6)) == "3.6"
    assert format_number(to_decimal(0.00001)) == "0.00001"  # never exponent form


@pytest.mark.parametrize("value", ["abc", "", "nan", "inf", float("nan"), float("inf"), True])
def test_to_decimal_rejects_non_numbers(value: Any) -> None:
    with pytest.raises(ModelError):
        to_decimal(value)


# --- boiler slot and schedule -----------------------------------------------------------------


def test_slot_create_accepts_loose_input() -> None:
    slot = BoilerSlot.create(7, "12:00:00", 3.9, "3.8")
    assert slot == BoilerSlot(7, time(12, 0), Decimal("3.9"), Decimal("3.8"))


def test_slot_equality_is_numeric_but_spelling_is_kept() -> None:
    three = BoilerSlot.create(6, "21:00:00", "3.0", "3")
    assert three == BoilerSlot.create(6, "21:00:00", "3", "3.0")
    assert format_number(three.minimum) == "3"
    assert format_number(three.target) == "3.0"


@pytest.mark.parametrize("day", [0, 8, -1, True])
def test_slot_rejects_a_day_outside_1_to_7(day: Any) -> None:
    with pytest.raises(ModelError, match="day of week"):
        BoilerSlot(day, time(6, 0), Decimal("3"), Decimal("2"))


def test_slot_rejects_times_that_are_not_whole_seconds_or_have_a_zone() -> None:
    with pytest.raises(ModelError, match="whole-second"):
        BoilerSlot(1, time(6, 0, 0, 5), Decimal("3"), Decimal("2"))
    with pytest.raises(ModelError, match="whole-second"):
        BoilerSlot(
            1, time(6, 0, tzinfo=datetime.now().astimezone().tzinfo), Decimal("3"), Decimal("2")
        )


def test_slot_rejects_floats_instead_of_decimals() -> None:
    not_a_decimal: Any = 3.0
    with pytest.raises(ModelError, match="finite Decimal"):
        BoilerSlot(1, time(6, 0), not_a_decimal, Decimal("2"))


def test_slot_is_frozen_and_hashable() -> None:
    slot = BoilerSlot.create(1, "06:00:00", 3, 2)
    field_name = "day"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(slot, field_name, 2)
    assert len({slot, BoilerSlot.create(1, "06:00:00", "3.0", "2.0")}) == 1


def test_schedule_is_sorted_by_day_then_time_and_exact_duplicates_are_dropped() -> None:
    a = BoilerSlot.create(2, "06:00:00", 4, 2)
    b = BoilerSlot.create(1, "18:00:00", 3, 2)
    c = BoilerSlot.create(1, "06:00:00", 3.6, 2)
    schedule = BoilerSchedule.of([a, b, c, a, b])
    assert schedule.slots == (c, b, a)
    assert len(schedule) == 3


def test_schedule_keeps_different_slots_that_share_a_time() -> None:
    # duplicates *with different values* are a validation problem, not silently merged here
    first = BoilerSlot.create(1, "06:00:00", 3, 2)
    second = BoilerSlot.create(1, "06:00:00", 4, 2)
    assert BoilerSchedule.of([second, first]).slots == (second, first)


def test_schedule_order_does_not_depend_on_input_order() -> None:
    slots = [BoilerSlot.create(d, f"{h:02d}:00:00", 3, 2) for d in (3, 1, 2) for h in (18, 6)]
    assert BoilerSchedule.of(slots) == BoilerSchedule.of(reversed(slots))


def test_schedule_constructor_refuses_an_unsorted_tuple() -> None:
    late = BoilerSlot.create(2, "06:00:00", 3, 2)
    early = BoilerSlot.create(1, "06:00:00", 3, 2)
    with pytest.raises(ModelError, match="sorted"):
        BoilerSchedule((late, early))


# --- timer entries and schedule ---------------------------------------------------------------


def test_timer_entry_valid() -> None:
    entry = TimerEntry(1, time(17, 34, 50), 1, 4)
    assert entry.sort_key == (1, time(17, 34, 50))


@pytest.mark.parametrize(
    ("day", "at", "state", "relay"),
    [
        (0, time(1, 0), 1, 4),
        (1, time(1, 0), 2, 4),
        (1, time(1, 0), 1, 8),
        (1, time(1, 0), 1, -1),
        (1, time(1, 0, 0, 1), 1, 4),
    ],
)
def test_timer_entry_rejects_bad_values(day: int, at: time, state: int, relay: int) -> None:
    with pytest.raises(ModelError):
        TimerEntry(day, at, state, relay)


def test_timer_schedule_sorts_and_deduplicates() -> None:
    off = TimerEntry(1, time(23, 0), 0, 4)
    on = TimerEntry(1, time(17, 34, 50), 1, 4)
    assert TimerSchedule.of([off, on, off]).entries == (on, off)
    with pytest.raises(ModelError, match="sorted"):
        TimerSchedule((off, on))


# --- manual files -----------------------------------------------------------------------------


def test_manual_override_is_auto_when_empty() -> None:
    assert ManualOverride().is_auto
    entry = ManualEntry(7, time(19, 0), Decimal("4"), Decimal("4"))
    assert not ManualOverride(entry).is_auto


def test_manual_entry_validates_the_expiry() -> None:
    with pytest.raises(ModelError):
        ManualEntry(0, time(19, 0), Decimal("4"), Decimal("4"))
    with pytest.raises(ModelError):
        ManualEntry(7, time(19, 0, 0, 1), Decimal("4"), Decimal("4"))


# --- boiler status ----------------------------------------------------------------------------


def test_status_parses_the_sample_answer() -> None:
    status = BoilerStatus.parse(SAMPLE_STATUS)
    assert status.mode == "Manual"
    assert status.heater_on is False
    assert status.active is True
    assert status.showers_linear == 3.43
    assert status.showers_thermo == 4.92
    assert status.temps == (58.06, 54.19, 61.44)
    assert (status.target, status.minimum, status.minutes_to_start) == (4.0, 4.0, 175)
    assert status.date == datetime(2026, 10, 24, 16, 40, 19, 707843)


def test_status_age_uses_the_local_time_of_the_pi() -> None:
    status = BoilerStatus.parse(SAMPLE_STATUS)
    now = datetime(2026, 10, 24, 16, 45, 19, 707843).astimezone()
    assert status.age(now.replace(hour=16, minute=45, second=19, microsecond=707843)) == timedelta(
        minutes=5
    )


@pytest.mark.parametrize("mode", ["Auto", "Manual", "Off", "Something new"])
def test_status_keeps_any_mode_text_as_it_is(mode: str) -> None:
    assert BoilerStatus.parse({**SAMPLE_STATUS, "Status": mode}).mode == mode


def test_status_heater_and_active_are_case_insensitive() -> None:
    status = BoilerStatus.parse({**SAMPLE_STATUS, "HeaterOnOff": "ON", "ActiveYesNo": "no"})
    assert status.heater_on is True
    assert status.active is False


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"CurrentTemp": [1, 2]}, "CurrentTemp"),
        ({"CurrentTemp": "abc"}, "CurrentTemp"),
        ({"CurrentTemp": [1, 2, None]}, "CurrentTemp"),
        ({"HeaterOnOff": "Maybe"}, "HeaterOnOff"),
        ({"ActiveYesNo": 1}, "ActiveYesNo"),
        ({"CurrentShowers": "3.4"}, "CurrentShowers"),
        ({"CurrentShowers": True}, "CurrentShowers"),
        ({"CurrentShowers": float("nan")}, "CurrentShowers"),
        ({"TimeToStartMin": 17.5}, "TimeToStartMin"),
        ({"Date": "yesterday"}, "Date"),
        ({"Date": 20261024}, "Date"),
        ({"Status": ""}, "Status"),
        ({"Status": None}, "Status"),
    ],
)
def test_status_with_a_bad_field_is_unreadable(change: dict[str, Any], key: str) -> None:
    with pytest.raises(StatusParseError, match=key):
        BoilerStatus.parse({**SAMPLE_STATUS, **change})


@pytest.mark.parametrize("key", sorted(SAMPLE_STATUS))
def test_status_with_a_missing_field_is_unreadable(key: str) -> None:
    payload = {k: v for k, v in SAMPLE_STATUS.items() if k != key}
    with pytest.raises(StatusParseError, match=f"{key}: missing"):
        BoilerStatus.parse(payload)


def test_connection_failure_placeholder_is_not_a_status() -> None:
    placeholder = {"Status": "fail to connect to Manager", "ActiveYesNo": "No"}
    with pytest.raises(StatusParseError):
        BoilerStatus.parse(placeholder)
