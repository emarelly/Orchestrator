"""Turning calendar events into the boiler and timer schedules (FR-CFG-1, FR-CFG-6, FR-CFG-8).

This is the successor of the second half of ``ReadGmailCalendar.ReadCalendar``. Every event title is
parsed (:mod:`orchestrator.event_titles`); then

* a **Boiler** event becomes a slot at the event's own day and start time;
* a **Timer** event becomes an ON entry at its start (or at its astro time) and an OFF entry at the
  end: an end clock time or astro time wins over a duration, an end earlier than the start means the
  next day (Saturday wraps to Sunday), and a duration is added on the absolute time line.

Nothing is guessed or clamped: an event that cannot be used (bad title, value above the heater
limit, all-day, no location for a sun time) is **skipped and reported** in ``problems`` while all
other events are still used. Ordinary events are ignored silently. The result is pure data; saving
and pushing it is the job of :mod:`orchestrator.push_queue`.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal

from orchestrator.astro import AstroError, Location, resolve_astro
from orchestrator.calendar_source import CalendarEvent, CalendarSource, week_window
from orchestrator.clock import Clock
from orchestrator.event_titles import BoilerCommand, TimerCommand, parse_title
from orchestrator.models import (
    BoilerSchedule,
    BoilerSlot,
    TimerEntry,
    TimerSchedule,
)
from orchestrator.relays import RelayNames
from orchestrator.validation import HARD_MAX_SHOWERS, check_levels
from orchestrator.weekdays import day_and_time, day_name

TITLE_SHOWN = 60


@dataclass(frozen=True, slots=True)
class CalendarSchedules:
    """The schedules built from one reading of the calendar, plus what went wrong."""

    boiler: BoilerSchedule
    timer: TimerSchedule
    problems: tuple[str, ...]  # events that were skipped, with the reason
    warnings: tuple[str, ...]  # things worth knowing that did not stop an event
    events_seen: int
    commands_found: int
    items: tuple["ScheduleItem", ...] = ()  # the events that were used, with their parsed command


@dataclass(frozen=True, slots=True)
class ScheduleItem:
    """A calendar event that became part of a schedule (the unit the UI edits)."""

    event: CalendarEvent
    command: BoilerCommand | TimerCommand


def _label(event: CalendarEvent) -> str:
    title = (
        event.summary if len(event.summary) <= TITLE_SHOWN else event.summary[:TITLE_SHOWN] + "..."
    )
    return f"{day_name(day_and_time(event.start)[0])} {event.start:%Y-%m-%d %H:%M} '{title}'"


def build_schedules(
    events: Iterable[CalendarEvent],
    *,
    tz: tzinfo,
    location: Location | None,
    relay_names: RelayNames | None = None,
    max_showers: Decimal = HARD_MAX_SHOWERS,
) -> CalendarSchedules:
    """Build both schedules from a list of events (see the module docstring for the rules)."""
    names = relay_names or RelayNames()
    slots: list[BoilerSlot] = []
    entries: list[TimerEntry] = []
    problems: list[str] = []
    warnings: list[str] = []
    items: list[ScheduleItem] = []
    seen = commands = 0
    for event in events:
        seen += 1
        parsed = parse_title(event.summary, names)
        label = _label(event)
        warnings.extend(f"{label}: {warning}" for warning in parsed.warnings)
        if parsed.problem:
            problems.append(f"{label}: {parsed.problem}")
            continue
        command = parsed.command
        if command is None:
            continue  # an ordinary event
        commands += 1
        if event.all_day:
            problems.append(f"{label}: an all-day event has no start time")
            continue
        if isinstance(command, BoilerCommand):
            used = _add_boiler(command, event, label, max_showers, slots, problems, warnings)
        else:
            used = _add_timer(command, event, label, tz, location, entries, problems)
        if used:
            items.append(ScheduleItem(event, command))
    return CalendarSchedules(
        BoilerSchedule.of(slots),
        TimerSchedule.of(entries),
        tuple(problems),
        tuple(warnings),
        seen,
        commands,
        tuple(items),
    )


def _add_boiler(
    command: BoilerCommand,
    event: CalendarEvent,
    label: str,
    max_showers: Decimal,
    slots: list[BoilerSlot],
    problems: list[str],
    warnings: list[str],
) -> bool:
    """Add the slot of a boiler event; False if the event was rejected."""
    checked = check_levels(command.target, command.minimum, max_showers, label=label)
    if checked.errors:
        problems.extend(checked.errors)  # rejected and reported, never clamped
        return False
    warnings.extend(checked.warnings)
    day, at = day_and_time(event.start)
    slots.append(BoilerSlot(day, at, command.target, command.minimum))
    return True


def _add_timer(
    command: TimerCommand,
    event: CalendarEvent,
    label: str,
    tz: tzinfo,
    location: Location | None,
    entries: list[TimerEntry],
    problems: list[str],
) -> bool:
    """Add the ON / OFF entries of a timer event; False if the event could not be resolved."""
    event_day = event.start.date()
    try:
        start = (
            event.start
            if command.start is None
            else resolve_astro(command.start, event_day, location, tz)
        )
        if command.end_clock is not None or command.end_astro is not None:
            end = _end_moment(command, event_day, tz, location, offset_days=0)
            if end.astimezone(UTC) < start.astimezone(UTC):  # earlier than the start: next day
                end = _end_moment(command, event_day, tz, location, offset_days=1)
        else:
            assert command.duration_s is not None  # guaranteed by the title parser
            end = (start.astimezone(UTC) + timedelta(seconds=command.duration_s)).astimezone(tz)
    except AstroError as error:
        problems.append(f"{label}: {error}")
        return False
    on_day, on_at = day_and_time(start)
    off_day, off_at = day_and_time(end)
    for relay in command.relays:
        entries.append(TimerEntry(on_day, on_at, 1, relay))
        entries.append(TimerEntry(off_day, off_at, 0, relay))
    return True


def _end_moment(
    command: TimerCommand,
    event_day: date,
    tz: tzinfo,
    location: Location | None,
    *,
    offset_days: int,
) -> datetime:
    day = event_day + timedelta(days=offset_days)
    if command.end_clock is not None:
        return datetime.combine(day, command.end_clock, tzinfo=tz)
    assert command.end_astro is not None
    return resolve_astro(command.end_astro, day, location, tz)


def read_schedules(
    source: CalendarSource,
    clock: Clock,
    *,
    location: Location | None,
    relay_names: RelayNames | None = None,
    max_showers: Decimal = HARD_MAX_SHOWERS,
) -> CalendarSchedules:
    """Read the current week (Sunday to Saturday) from the calendar and build the schedules.

    Raises :class:`~orchestrator.calendar_source.CalendarError` if the calendar cannot be read; the
    caller then keeps the last known schedules.
    """
    start, end = week_window(clock.now())
    reading = source.read(start, end)
    built = build_schedules(
        reading.events,
        tz=clock.tz,
        location=location,
        relay_names=relay_names,
        max_showers=max_showers,
    )
    skipped = tuple(f"calendar: {note}" for note in reading.skipped)
    return CalendarSchedules(
        built.boiler,
        built.timer,
        built.problems,
        built.warnings + skipped,
        built.events_seen,
        built.commands_found,
        built.items,
    )
