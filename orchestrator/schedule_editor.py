"""Editing the schedules from the UI by changing the real calendar (FR-CFG-4, FR-CFG-7, FR-CFG-8).

The calendar stays the single place where the schedule lives: a UI edit becomes a calendar edit and
is then read back like any other change. The rules that keep this safe:

* **Off by default.** Nothing is written unless ``[calendar] write_back`` is true.
* **Only what can be edited safely.** Events this program created (marked in Google) can be edited
  freely. An event made by hand is edited only if it is a plain event or a *weekly series on a
  single weekday*; a series on several weekdays, an every-two-weeks series, a daily or monthly
  series or an all-day event is refused with an explanation, because changing it would change days
  the user did not touch. Editing or deleting changes the whole series (the weekly pattern).
* **Last modification wins (FR-CFG-7).** If the calendar event changed after the UI loaded it, the
  edit is applied only if it is newer than that change; otherwise it is a conflict and nothing is
  written. Either way both sides are logged.
* **Validated before writing.** The new title must read back as exactly the command that was
  meant, and a boiler command is checked against the limit of 5 (no clamping).
* **Every attempt is audited** with who, what and the result; expected problems come back as an
  :class:`EditResult`, not as exceptions.
"""

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from orchestrator.calendar_schedules import ScheduleItem
from orchestrator.calendar_source import CalendarAuthError, CalendarError
from orchestrator.calendar_writer import DEFAULT_LENGTH, RemoteEvent
from orchestrator.clock import Clock
from orchestrator.event_titles import (
    BoilerCommand,
    TimerCommand,
    UnrepresentableError,
    format_boiler_title,
    format_timer_title,
)
from orchestrator.logging_setup import audit, correlation, kv
from orchestrator.models import ModelError
from orchestrator.relays import RelayNames
from orchestrator.validation import HARD_MAX_SHOWERS, check_levels
from orchestrator.weekdays import boiler_day, day_name, week_start

logger = logging.getLogger(__name__)

_BYDAY = ("SU", "MO", "TU", "WE", "TH", "FR", "SA")  # index = boiler day - 1
Command = BoilerCommand | TimerCommand


class EditOutcome(StrEnum):
    APPLIED = "applied"
    REFUSED = "refused"  # not allowed, or the input is not acceptable; nothing was written
    CONFLICT = "conflict"  # the calendar is newer than the edit; nothing was written
    FAILED = "failed"  # the calendar could not be reached or refused


@dataclass(frozen=True, slots=True)
class EditResult:
    outcome: EditOutcome
    message: str
    event_id: str | None = None
    correlation_id: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is EditOutcome.APPLIED


class EventWriter(Protocol):
    """What the editor needs from the calendar writer."""

    def get(self, event_id: str) -> RemoteEvent: ...

    def create(
        self,
        *,
        summary: str,
        start: datetime,
        recurrence: Sequence[str] = (),
        length: timedelta = DEFAULT_LENGTH,
    ) -> RemoteEvent: ...

    def patch(
        self,
        event_id: str,
        *,
        summary: str | None = None,
        start: datetime | None = None,
        recurrence: Sequence[str] | None = None,
        length: timedelta = DEFAULT_LENGTH,
    ) -> RemoteEvent: ...

    def delete(self, event_id: str) -> None: ...


def _event_length(command: Command, start: datetime) -> timedelta:
    """The length the calendar event needs so that its own end is right for ``command``.

    A timer without a sun or moon time runs from the start to the end of the event, so the event is
    made as long as the timer (an end before the start means the next day). Everything else keeps
    the default length: the title, not the event end, holds the times.
    """
    if isinstance(command, TimerCommand) and command.start is None and command.end_astro is None:
        if command.end_clock is not None:
            end = datetime.combine(start.date(), command.end_clock, start.tzinfo)
            if end <= start:
                end += timedelta(days=1)
            return end - start  # wall-clock difference, as the writer adds it back
        if command.duration_s is not None:
            return timedelta(seconds=command.duration_s)
    return DEFAULT_LENGTH


class _Stop(Exception):
    """Ends an edit early with a ready-made result."""

    def __init__(self, outcome: EditOutcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.message = message


class ScheduleEditor:
    """Applies schedule edits from the UI to the calendar."""

    def __init__(
        self,
        writer: EventWriter,
        clock: Clock,
        *,
        enabled: bool,
        relay_names: RelayNames | None = None,
        max_showers: Decimal = HARD_MAX_SHOWERS,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        self._writer = writer
        self._clock = clock
        self._enabled = enabled
        self._names = relay_names or RelayNames()
        self._max_showers = max_showers
        self._on_changed = on_changed
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """False while write-back is off or the calendar token is read-only."""
        return self._enabled

    # --- the three edits ----------------------------------------------------------------------

    def create(
        self, command: Command, start: datetime, days: Sequence[int], *, who: str
    ) -> EditResult:
        """Add a weekly event on the given boiler days (1 = Sunday) at the time of ``start``."""

        def work() -> tuple[str, str]:
            title = self._title(command)
            weekdays = _days(days)
            # the series starts in the week of ``start`` (not after it), so that it is part of
            # the schedule built from the current week at once
            first = _first_date(week_start(start.date()), weekdays)
            moment = datetime.combine(
                first, start.time().replace(microsecond=0, fold=0), start.tzinfo
            )
            event = self._writer.create(
                summary=title,
                start=moment,
                recurrence=[_rule(weekdays)],
                length=_event_length(command, moment),
            )
            return event.event_id, f"Created '{title}' on {_days_text(weekdays)}"

        return self._run("create", who, None, work)

    def update(
        self,
        item: ScheduleItem,
        command: Command,
        *,
        who: str,
        start_time: time | None = None,
        days: Sequence[int] | None = None,
        edited_at: datetime | None = None,
    ) -> EditResult:
        """Change the command (and optionally the time or, for our own events, the days)."""

        def work() -> tuple[str, str]:
            title = self._title(command)
            master_id, remote = self._master(item)
            self._check_allowed(remote)
            self._check_conflict(item, remote, edited_at, master_id)
            start: datetime | None = None
            recurrence: list[str] | None = None
            if days is not None:
                if not remote.managed:
                    raise _Stop(
                        EditOutcome.REFUSED,
                        "the days of an event made in Google Calendar can only be changed there",
                    )
                weekdays = _days(days)
                recurrence = [_rule(weekdays)]
                base = remote.start or item.event.start
                clock = start_time or base.time().replace(microsecond=0, fold=0)
                start = datetime.combine(_first_date(base.date(), weekdays), clock, base.tzinfo)
            elif start_time is not None:
                base = remote.start or item.event.start
                start = datetime.combine(base.date(), start_time, base.tzinfo)
                recurrence = list(remote.recurrence) if remote.recurrence else None
            elif _event_length(command, item.event.start) != DEFAULT_LENGTH:
                # the event's own end is the timer's end: write start and end again
                start = remote.start or item.event.start
                recurrence = list(remote.recurrence) if remote.recurrence else None
            length = _event_length(command, start) if start is not None else DEFAULT_LENGTH
            self._writer.patch(
                master_id, summary=title, start=start, recurrence=recurrence, length=length
            )
            return master_id, f"Changed '{remote.summary}' to '{title}'"

        return self._run("update", who, item.event.event_id, work)

    def delete(
        self, item: ScheduleItem, *, who: str, edited_at: datetime | None = None
    ) -> EditResult:
        """Delete the event (the whole weekly series) behind a schedule item."""

        def work() -> tuple[str, str]:
            master_id, remote = self._master(item)
            self._check_allowed(remote)
            self._check_conflict(item, remote, edited_at, master_id)
            self._writer.delete(master_id)
            return master_id, f"Deleted '{remote.summary}'"

        return self._run("delete", who, item.event.event_id, work)

    # --- rules --------------------------------------------------------------------------------

    def _title(self, command: Command) -> str:
        try:
            if isinstance(command, BoilerCommand):
                checked = check_levels(command.target, command.minimum, self._max_showers)
                if checked.errors:
                    raise _Stop(EditOutcome.REFUSED, "; ".join(checked.errors))
                return format_boiler_title(command)
            return format_timer_title(command, self._names)
        except UnrepresentableError as error:
            raise _Stop(EditOutcome.REFUSED, str(error)) from None

    def _master(self, item: ScheduleItem) -> tuple[str, RemoteEvent]:
        master_id = item.event.recurring_event_id or item.event.event_id
        return master_id, self._writer.get(master_id)

    def _check_allowed(self, remote: RemoteEvent) -> None:
        if remote.managed:
            return
        if remote.all_day:
            raise _Stop(EditOutcome.REFUSED, "an all-day event cannot be edited from here")
        problem = _series_problem(remote.recurrence)
        if problem:
            raise _Stop(
                EditOutcome.REFUSED,
                f"'{remote.summary}' is {problem}; edit it in Google Calendar, where you can "
                "change a single day without touching the others",
            )

    def _check_conflict(
        self, item: ScheduleItem, remote: RemoteEvent, edited_at: datetime | None, event_id: str
    ) -> None:
        known, current = item.event.updated, remote.updated
        if known is None or current is None or current.astimezone(UTC) <= known.astimezone(UTC):
            return
        moment = (edited_at or self._clock.now()).astimezone(UTC)
        if current.astimezone(UTC) >= moment:
            logger.warning(kv("schedule_edit_conflict", calendar_event=event_id, winner="calendar"))
            raise _Stop(
                EditOutcome.CONFLICT,
                "the event was changed in the calendar after this page was loaded and that change "
                "is newer; reload the page and edit again",
            )
        logger.warning(kv("schedule_edit_conflict", calendar_event=event_id, winner="ui"))
        audit(
            "schedule_conflict",
            calendar_event=event_id,
            winner="ui",
            calendar_changed=current.isoformat(),
        )

    # --- running one edit -----------------------------------------------------------------------

    def _run(
        self, action: str, who: str, event_id: str | None, work: Callable[[], tuple[str, str]]
    ) -> EditResult:
        with self._lock, correlation() as cid:
            outcome, message, written = EditOutcome.FAILED, "", event_id
            try:
                if not self._enabled:
                    raise _Stop(
                        EditOutcome.REFUSED,
                        "writing to the calendar is switched off ([calendar] write_back = false)",
                    )
                written, message = work()
                outcome = EditOutcome.APPLIED
            except _Stop as stop:
                outcome, message = stop.outcome, stop.message
            except CalendarAuthError as error:
                outcome, message = EditOutcome.FAILED, f"Google authorization is needed: {error}"
            except CalendarError as error:
                outcome, message = EditOutcome.FAILED, f"the calendar could not be changed: {error}"
            audit(
                "schedule_edit",
                who=who,
                channel="ui",
                action=action,
                calendar_event=written,
                result=outcome,
                detail=None if outcome is EditOutcome.APPLIED else message,
            )
            (logger.info if outcome is EditOutcome.APPLIED else logger.warning)(
                kv("schedule_edit", action=action, result=outcome)
            )
            result = EditResult(outcome, message, written, cid)
        if result.ok and self._on_changed is not None:
            self._on_changed()
        return result


# --- helpers -----------------------------------------------------------------------------------


def _days(days: Sequence[int]) -> list[int]:
    try:
        names = [day_name(day) for day in days]
    except ModelError as error:
        raise _Stop(EditOutcome.REFUSED, str(error)) from None
    if not names:
        raise _Stop(EditOutcome.REFUSED, "at least one day is needed")
    return sorted(set(days))


def _days_text(days: Sequence[int]) -> str:
    return ", ".join(day_name(day) for day in days)


def _rule(days: Sequence[int]) -> str:
    return "RRULE:FREQ=WEEKLY;BYDAY=" + ",".join(_BYDAY[day - 1] for day in days)


def _first_date(start: date, days: Sequence[int]) -> date:
    for offset in range(7):
        candidate = start + timedelta(days=offset)
        if boiler_day(candidate) in days:
            return candidate
    raise _Stop(EditOutcome.REFUSED, "no matching day")  # unreachable: days is not empty


def _series_problem(recurrence: Sequence[str]) -> str | None:
    """Describe why a hand-made event cannot be edited safely, or ``None`` if it can."""
    for line in recurrence:
        upper = line.upper()
        if upper.startswith("RDATE"):
            return "a series with extra dates"
        if not upper.startswith("RRULE:"):
            continue  # EXDATE lines only skip single days: fine
        fields = dict(part.split("=", 1) for part in upper[6:].split(";") if "=" in part)
        if fields.get("FREQ") != "WEEKLY":
            return "not a weekly series"
        if fields.get("INTERVAL", "1") != "1":
            return "a series that does not repeat every week"
        if len([d for d in fields.get("BYDAY", "").split(",") if d]) > 1:
            return "a series on several weekdays"
    return None
