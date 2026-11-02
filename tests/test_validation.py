"""Validation rules (FR-CFG-8, section 6): the same checks for every input channel."""

from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from orchestrator.models import (
    BoilerSchedule,
    BoilerSlot,
    ManualEntry,
    TimerEntry,
    TimerSchedule,
)
from orchestrator.schedule_files import parse_boiler_schedule, parse_timer_schedule
from orchestrator.validation import (
    HARD_MAX_SHOWERS,
    MANUAL_RELAYS,
    ValidationError,
    ValidationResult,
    check_boiler_schedule,
    check_levels,
    check_manual_entry,
    check_manual_relay,
    check_timer_schedule,
)

ROOT = Path(__file__).resolve().parent.parent
D = Decimal


# --- the real files are acceptable ------------------------------------------------------------


def test_the_real_cal_json_is_valid_and_its_min_above_target_entries_are_only_warnings() -> None:
    schedule = parse_boiler_schedule((ROOT / "Cal.json").read_bytes())
    result = check_boiler_schedule(schedule)
    assert result.ok
    expected = [s for s in schedule.slots if s.minimum > s.target]
    assert expected  # the file really does use Min > Target (for example Sunday 13:00: 3.0 / 3.5)
    assert len(result.warnings) == len(expected)
    assert any("Sunday 13:00:00: Min 3.5 is above Target 3.0" in w for w in result.warnings)


def test_the_real_time_cal_json_is_valid_without_warnings() -> None:
    schedule = parse_timer_schedule((ROOT / "TimeCal.json").read_bytes())
    result = check_timer_schedule(schedule)
    assert result.ok
    assert result.warnings == ()


# --- showers: 0 <= value <= 5 on every channel ------------------------------------------------


@pytest.mark.parametrize("value", ["0", "0.0", "2.5", "4.9", "5", "5.0"])
def test_values_from_0_to_5_are_accepted(value: str) -> None:
    assert check_levels(D(value), D(value)).ok


@pytest.mark.parametrize("value", ["5.1", "5.01", "6", "10", "100"])
def test_values_above_5_are_rejected_for_target_and_for_min(value: str) -> None:
    high = check_levels(D(value), D("2"))
    assert not high.ok and "Target" in high.errors[0] and "above the limit of 5" in high.errors[0]
    low = check_levels(D("3"), D(value))
    assert not low.ok and "Min" in low.errors[0]


@pytest.mark.parametrize("value", ["-0.1", "-1", "-5"])
def test_negative_values_are_rejected(value: str) -> None:
    assert not check_levels(D(value), D("2")).ok
    assert not check_levels(D("3"), D(value)).ok


def test_a_caller_cannot_raise_the_limit_above_5() -> None:
    assert not check_levels(D("7"), D("2"), max_showers=D("10")).ok
    assert D("5") == HARD_MAX_SHOWERS


def test_a_caller_can_lower_the_limit() -> None:
    assert check_levels(D("4"), D("2"), max_showers=D("4")).ok
    assert not check_levels(D("4.5"), D("2"), max_showers=D("4")).ok


def test_both_problems_are_reported_together() -> None:
    result = check_levels(D("6"), D("-1"), label="Monday 06:00:00")
    assert len(result.errors) == 2
    assert all(error.startswith("Monday 06:00:00: ") for error in result.errors)


def test_min_above_target_is_a_warning_not_an_error() -> None:
    result = check_levels(D("3.0"), D("3.5"))
    assert result.ok
    assert result.warnings == ("Min 3.5 is above Target 3.0; the heater will keep Min",)


def test_equal_target_and_min_is_fine() -> None:
    result = check_levels(D("4"), D("4"))
    assert result.ok and not result.warnings


def test_a_manual_override_is_checked_against_the_same_limit() -> None:
    assert check_manual_entry(ManualEntry(7, time(19, 0), D("5"), D("5"))).ok
    result = check_manual_entry(ManualEntry(7, time(19, 0), D("5.5"), D("5.5")))
    assert not result.ok and result.errors[0].startswith("manual override: ")


# --- boiler schedule --------------------------------------------------------------------------


def test_every_slot_of_a_schedule_is_checked_and_named() -> None:
    schedule = BoilerSchedule.of(
        [
            BoilerSlot.create(1, "06:00:00", 3, 2),
            BoilerSlot.create(2, "18:30:00", 6, 2),
            BoilerSlot.create(3, "07:00:00", 3, -1),
        ]
    )
    result = check_boiler_schedule(schedule)
    assert not result.ok
    assert any(e.startswith("Monday 18:30:00: Target 6") for e in result.errors)
    assert any(e.startswith("Tuesday 07:00:00: Min -1") for e in result.errors)


def test_two_different_slots_at_the_same_time_are_an_error() -> None:
    schedule = BoilerSchedule.of(
        [BoilerSlot.create(1, "06:00:00", 3, 2), BoilerSlot.create(1, "06:00:00", 4, 2)]
    )
    result = check_boiler_schedule(schedule)
    assert result.errors == ("Sunday 06:00:00: 2 different slots at the same time",)


def test_an_empty_boiler_schedule_is_an_error() -> None:
    assert check_boiler_schedule(BoilerSchedule()).errors == ("the boiler schedule has no slots",)


# --- timer schedule ---------------------------------------------------------------------------


def entry(day: int, at: str, state: int, relay: int = 4) -> TimerEntry:
    hour, minute, second = (int(part) for part in at.split(":"))
    return TimerEntry(day, time(hour, minute, second), state, relay)


def test_a_relay_outside_the_list_is_an_error() -> None:
    schedule = TimerSchedule.of([entry(1, "17:00:00", 1, 5), entry(1, "23:00:00", 0, 5)])
    result = check_timer_schedule(schedule, relays={4})
    assert not result.ok
    assert result.errors[0] == "Sun 17:00:00: relay 5 is not in the relay list"


def test_a_relay_switched_on_and_off_at_the_same_moment_is_an_error() -> None:
    schedule = TimerSchedule.of([entry(1, "17:00:00", 1), entry(1, "17:00:00", 0)])
    assert "relay 4 is switched on and off at once" in check_timer_schedule(schedule).errors[0]


def test_on_off_entries_that_alternate_give_no_warning() -> None:
    schedule = TimerSchedule.of(
        [
            entry(1, "17:00:00", 1),
            entry(1, "23:00:00", 0),
            entry(2, "17:00:00", 1),
            entry(2, "23:00:00", 0),
        ]
    )
    assert check_timer_schedule(schedule).warnings == ()


def test_two_on_entries_in_a_row_are_a_warning() -> None:
    schedule = TimerSchedule.of([entry(1, "17:00:00", 1), entry(2, "17:00:00", 1)])
    result = check_timer_schedule(schedule)
    assert result.ok
    assert result.warnings == ("relay 4: two ON entries in a row (Sun 17:00:00 and Mon 17:00:00)",)


def test_the_check_wraps_around_the_week() -> None:
    # off on Saturday night, then off again on Sunday: two OFF entries across the week boundary
    schedule = TimerSchedule.of(
        [entry(1, "06:00:00", 0), entry(4, "17:00:00", 1), entry(7, "23:00:00", 0)]
    )
    warnings = check_timer_schedule(schedule).warnings
    assert warnings == ("relay 4: two OFF entries in a row (Sat 23:00:00 and Sun 06:00:00)",)


def test_a_relay_with_a_single_entry_is_a_warning() -> None:
    result = check_timer_schedule(TimerSchedule.of([entry(1, "17:00:00", 1)]))
    assert result.warnings == ("relay 4: only one entry (ON Sun 17:00:00)",)


def test_relays_are_checked_independently() -> None:
    schedule = TimerSchedule.of(
        [
            entry(1, "17:00:00", 1, 4),
            entry(1, "18:00:00", 1, 2),
            entry(1, "23:00:00", 0, 4),
            entry(1, "23:30:00", 0, 2),
        ]
    )
    assert check_timer_schedule(schedule).warnings == ()


def test_an_empty_timer_schedule_is_an_error() -> None:
    assert check_timer_schedule(TimerSchedule()).errors == ("the timer schedule has no entries",)


# --- manual relay commands --------------------------------------------------------------------


@pytest.mark.parametrize("relay", sorted(MANUAL_RELAYS))
def test_relays_2_to_7_are_allowed_for_manual_switching(relay: int) -> None:
    assert check_manual_relay(relay, 600).ok


@pytest.mark.parametrize("relay", [0, 1, 8, -1, 100])
def test_relays_0_1_and_unknown_ones_are_refused(relay: int) -> None:
    result = check_manual_relay(relay, 600)
    assert not result.ok
    assert "cannot be switched manually" in result.errors[0]


@pytest.mark.parametrize(
    ("seconds", "allowed"),
    [(10, False), (11, True), (86_400, True), (86_401, False), (1, False), (0, True), (-5, True)],
)
def test_manual_relay_duration_rules(seconds: int, allowed: bool) -> None:
    assert check_manual_relay(4, seconds).ok is allowed


def test_booleans_are_not_accepted_as_relay_or_duration() -> None:
    assert not check_manual_relay(True, 600).ok
    assert not check_manual_relay(4, True).ok


# --- the result object ------------------------------------------------------------------------


def test_raise_if_errors_lists_every_problem() -> None:
    result = ValidationResult(errors=("first problem", "second problem"))
    with pytest.raises(ValidationError) as caught:
        result.raise_if_errors()
    assert caught.value.problems == ("first problem", "second problem")
    assert "first problem; second problem" in str(caught.value)


def test_raise_if_errors_is_silent_when_only_warnings_exist() -> None:
    ValidationResult(warnings=("just a note",)).raise_if_errors()
