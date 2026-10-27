"""Day-of-week conversion, in one place (CS-13).

The boiler and timer services number the days 1 = Sunday ... 7 = Saturday and the week wraps from
7 back to 1. Python's ``weekday()`` starts on Monday at 0 and ``isoweekday()`` on Monday at 1, so
every conversion goes through :func:`boiler_day`; no other module may do this arithmetic itself.

Moments are timezone-aware datetimes in the configured zone (see :mod:`orchestrator.clock`).
Adding a duration (the 3 hours of a manual override) is done on the absolute time line, so a
daylight-saving change inside the interval is followed instead of being ignored.
"""

from datetime import UTC, date, datetime, time, timedelta

from orchestrator.models import DAY_MAX, DAY_MIN, ModelError

SECONDS_PER_DAY = 24 * 60 * 60
SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _check(day: int) -> None:
    if isinstance(day, bool) or not isinstance(day, int) or not DAY_MIN <= day <= DAY_MAX:
        raise ModelError(f"day of week must be {DAY_MIN}..{DAY_MAX} (1 = Sunday), got {day!r}")


def boiler_day(value: date) -> int:
    """Return the boiler day number (1 = Sunday ... 7 = Saturday) of a date or datetime."""
    return value.isoweekday() % 7 + 1


def week_start(day: date) -> date:
    """Return the Sunday on or before `day`: the first day of its week (day 1)."""
    return day - timedelta(days=day.isoweekday() % 7)


def day_name(day: int) -> str:
    """Return the English name of a boiler day number."""
    _check(day)
    return DAY_NAMES[day - 1]


def next_day(day: int) -> int:
    """Return the following day; Saturday (7) is followed by Sunday (1)."""
    _check(day)
    return day % 7 + 1


def previous_day(day: int) -> int:
    """Return the preceding day; Sunday (1) is preceded by Saturday (7)."""
    _check(day)
    return (day - 2) % 7 + 1


def day_and_time(moment: datetime) -> tuple[int, time]:
    """Return the boiler day and the whole-second wall-clock time of a moment."""
    return boiler_day(moment), moment.time().replace(microsecond=0, fold=0)


def expiry_after(now: datetime, delta: timedelta) -> tuple[int, time]:
    """Return the boiler day and time ``delta`` after ``now`` (the expiry of a manual override).

    The addition is absolute, so the result is right across a daylight-saving change. The time is
    cut to whole seconds. ``now`` must be timezone-aware.
    """
    zone = now.tzinfo
    if zone is None:
        raise ModelError("a timezone-aware time is required")
    later = (now.astimezone(UTC) + delta).astimezone(zone)
    return day_and_time(later)


def week_offset(day: int, at: time) -> int:
    """Return the seconds from Sunday 00:00 to the given day and time (0 .. 604799)."""
    _check(day)
    return (day - 1) * SECONDS_PER_DAY + at.hour * 3600 + at.minute * 60 + at.second


def week_distance(start: int, end: int) -> int:
    """Return the seconds to wait from one week offset to the next time ``end`` comes round."""
    return (end - start) % SECONDS_PER_WEEK
