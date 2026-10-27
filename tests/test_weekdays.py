"""Day-of-week conversion and week arithmetic (CS-13, FR-MAN-3)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from orchestrator.models import ModelError
from orchestrator.weekdays import (
    SECONDS_PER_WEEK,
    boiler_day,
    day_and_time,
    day_name,
    expiry_after,
    next_day,
    previous_day,
    week_distance,
    week_offset,
)

JERUSALEM = ZoneInfo("Asia/Jerusalem")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 10, 25), 1),  # Sunday
        (date(2026, 10, 26), 2),
        (date(2026, 10, 27), 3),
        (date(2026, 10, 28), 4),
        (date(2026, 10, 29), 5),
        (date(2026, 10, 30), 6),  # Friday
        (date(2026, 10, 31), 7),  # Saturday
        (date(2026, 10, 24), 7),  # the Saturday of the manual.json example
    ],
)
def test_boiler_day_numbers_sunday_as_1_and_saturday_as_7(value: date, expected: int) -> None:
    assert boiler_day(value) == expected


def test_boiler_day_agrees_with_the_legacy_formula_for_every_date_of_two_years() -> None:
    # ReadGmailCalendar.py: "if date.weekday() == 6: day = 1 else: day = date.weekday() + 2"
    current = date(2026, 1, 1)
    for _ in range(730):
        legacy = 1 if current.weekday() == 6 else current.weekday() + 2
        assert boiler_day(current) == legacy, current
        current += timedelta(days=1)


def test_boiler_day_accepts_a_datetime() -> None:
    assert boiler_day(datetime(2026, 10, 24, 23, 59, 59, tzinfo=JERUSALEM)) == 7


def test_week_wraps_from_saturday_to_sunday_and_back() -> None:
    assert [next_day(day) for day in range(1, 8)] == [2, 3, 4, 5, 6, 7, 1]
    assert [previous_day(day) for day in range(1, 8)] == [7, 1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("bad", [0, 8, -1, True])
def test_day_helpers_reject_numbers_outside_1_to_7(bad: int) -> None:
    for function in (next_day, previous_day, day_name):
        with pytest.raises(ModelError, match="day of week"):
            function(bad)


def test_day_names() -> None:
    assert day_name(1) == "Sunday"
    assert day_name(7) == "Saturday"


def test_day_and_time_cuts_to_whole_seconds() -> None:
    moment = datetime(2026, 10, 24, 16, 0, 0, 900_000, tzinfo=JERUSALEM)
    assert day_and_time(moment) == (7, time(16, 0, 0))


# --- manual override expiry (FR-MAN-3) --------------------------------------------------------


def test_the_manual_json_example_saturday_1600_plus_3h_is_saturday_1900() -> None:
    now = datetime(2026, 10, 24, 16, 0, 0, tzinfo=JERUSALEM)
    assert expiry_after(now, timedelta(hours=3)) == (7, time(19, 0, 0))


def test_expiry_rolls_over_midnight_into_the_next_day() -> None:
    friday = datetime(2026, 10, 23, 22, 0, tzinfo=JERUSALEM)
    assert expiry_after(friday, timedelta(hours=3)) == (7, time(1, 0, 0))


def test_expiry_rolls_over_from_saturday_to_sunday() -> None:
    # Saturday 2026-10-31 is a normal week (no clock change): 23:00 + 3 h = Sunday 02:00
    saturday = datetime(2026, 10, 31, 23, 0, tzinfo=JERUSALEM)
    assert expiry_after(saturday, timedelta(hours=3)) == (1, time(2, 0, 0))


def test_expiry_on_the_night_the_clocks_go_back_is_one_hour_earlier_on_the_wall_clock() -> None:
    # Saturday 2026-10-24 23:00 (+03:00) + 3 h is 23:00 UTC = Sunday 01:00 (+02:00), not 02:00
    saturday = datetime(2026, 10, 24, 23, 0, tzinfo=JERUSALEM)
    day, at = expiry_after(saturday, timedelta(hours=3))
    assert (day, at) == (1, time(1, 0, 0))
    assert at.fold == 0  # the repeated-hour flag is not carried into the file value


def test_expiry_across_the_spring_clock_change_is_absolute() -> None:
    # Thursday 2026-03-26 23:00 (+02:00) + 24 h: clocks jump forward on Friday 02:00, so the
    # result is Saturday 00:00 (+03:00). Wall-clock arithmetic would wrongly give Friday 23:00.
    start = datetime(2026, 3, 26, 23, 0, tzinfo=JERUSALEM)
    assert expiry_after(start, timedelta(hours=24)) == (7, time(0, 0, 0))


def test_expiry_across_the_autumn_clock_change_is_absolute() -> None:
    # Saturday 2026-10-24 23:00 (+03:00) + 24 h: clocks go back on Sunday 02:00, so the result is
    # Sunday 22:00 (+02:00). Wall-clock arithmetic would wrongly give Sunday 23:00.
    start = datetime(2026, 10, 24, 23, 0, tzinfo=JERUSALEM)
    assert expiry_after(start, timedelta(hours=24)) == (1, time(22, 0, 0))


def test_expiry_inside_the_repeated_hour_gives_the_same_wall_clock_time_twice() -> None:
    # 01:30 happens twice on 2026-10-25; the boiler file can only say "Sunday 01:30:00"
    start = datetime(2026, 10, 25, 0, 30, tzinfo=JERUSALEM)
    assert expiry_after(start, timedelta(hours=1)) == (1, time(1, 30, 0))
    assert expiry_after(start, timedelta(hours=2)) == (1, time(1, 30, 0))


def test_expiry_requires_a_timezone_aware_time() -> None:
    with pytest.raises(ModelError, match="timezone-aware"):
        expiry_after(datetime(2026, 10, 24, 16, 0), timedelta(hours=3))


# --- position in the weekly cycle -------------------------------------------------------------


def test_week_offset_runs_from_sunday_midnight_to_saturday_last_second() -> None:
    assert week_offset(1, time(0, 0, 0)) == 0
    assert week_offset(2, time(0, 0, 0)) == 86_400
    assert week_offset(7, time(23, 59, 59)) == SECONDS_PER_WEEK - 1


def test_week_distance_goes_forward_and_wraps_around_the_week() -> None:
    saturday_night = week_offset(7, time(23, 0, 0))
    sunday_morning = week_offset(1, time(1, 0, 0))
    assert week_distance(saturday_night, sunday_morning) == 2 * 3600
    assert week_distance(sunday_morning, saturday_night) == SECONDS_PER_WEEK - 2 * 3600
    assert week_distance(sunday_morning, sunday_morning) == 0
