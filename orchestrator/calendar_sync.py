"""The calendar sync loop: calendar -> schedules -> push queue (FR-CFG-1, FR-CFG-4, FR-CFG-7).

Every few minutes (and at once after a UI edit) the current week is read, turned into the boiler and
timer schedules and handed to the push queue. What the UI shows is simply the **latest report** of
this loop, so a change made in Google Calendar and a change made through the UI both appear there
after the next read: the calendar is the one place where the schedule lives.

Safety rules:

* if the calendar cannot be read, nothing changes: the last good schedules stay in force and the
  report says why (``auth_lost`` for a lost authorization, FR-GOOG-7);
* a schedule is pushed only if it is valid (limit of 5, relays known, no empty schedule); a week
  without any boiler event does *not* erase the schedule on the boiler Pi;
* a change is described in the legacy words ("The following dates were added ...") for the admin
  e-mail, and each state change is logged once.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from orchestrator.astro import Location
from orchestrator.calendar_schedules import CalendarSchedules, read_schedules
from orchestrator.calendar_source import CalendarAuthError, CalendarError, CalendarSource
from orchestrator.clock import Clock
from orchestrator.logging_setup import kv
from orchestrator.models import (
    BoilerSchedule,
    BoilerSlot,
    TimerEntry,
    TimerSchedule,
    format_number,
    format_time,
)
from orchestrator.push_queue import SubmitResult
from orchestrator.relays import RelayNames
from orchestrator.validation import (
    HARD_MAX_SHOWERS,
    ValidationError,
    check_boiler_schedule,
    check_timer_schedule,
)
from orchestrator.weekdays import day_name

logger = logging.getLogger(__name__)


class SchedulePusher(Protocol):
    """What the sync needs from the push queue."""

    def set_boiler_schedule(self, schedule: BoilerSchedule) -> SubmitResult: ...

    def set_timer_schedule(self, schedule: TimerSchedule) -> SubmitResult: ...


@dataclass(frozen=True, slots=True)
class SyncReport:
    """The outcome of one sync cycle (immutable; the UI shows the latest one)."""

    at: datetime
    ok: bool  # the calendar was read in this cycle
    schedules: CalendarSchedules | None  # the last good reading (also when this cycle failed)
    boiler_changed: bool
    timer_changed: bool
    diff: str  # legacy-style description of what changed, empty if nothing did
    problems: tuple[str, ...]  # skipped events and schedules that were not pushed
    error: str | None = None  # why the calendar could not be read
    auth_lost: bool = False
    file_changed: bool = False  # a local schedule file was rewritten in this cycle (FR-LEG-2)


# --- describing a change --------------------------------------------------------------------------


def _slot_text(slot: BoilerSlot) -> str:
    return (
        f"boiler {day_name(slot.day)} {format_time(slot.start)} "
        f"target {format_number(slot.target)} min {format_number(slot.minimum)}"
    )


def _entry_text(entry: TimerEntry) -> str:
    state = "on" if entry.state else "off"
    return f"timer relay {entry.relay} {state} {day_name(entry.day)} {format_time(entry.at)}"


def describe_changes(old: CalendarSchedules | None, new: CalendarSchedules) -> str:
    """List added and removed slots and entries in the words of the legacy e-mail."""
    old_slots = set(old.boiler.slots) if old else set()
    old_entries = set(old.timer.entries) if old else set()
    new_slots, new_entries = set(new.boiler.slots), set(new.timer.entries)
    added = [_slot_text(s) for s in new.boiler.slots if s not in old_slots]
    added += [_entry_text(e) for e in new.timer.entries if e not in old_entries]
    removed = [_slot_text(s) for s in (old.boiler.slots if old else ()) if s not in new_slots]
    removed += [_entry_text(e) for e in (old.timer.entries if old else ()) if e not in new_entries]
    lines: list[str] = []
    if added:
        lines += ["The following dates were added to the calendar:", *(f"  {a}" for a in added)]
    if removed:
        lines += [
            "The following dates were removed from the calendar:",
            *(f"  {r}" for r in removed),
        ]
    return "\r\n".join(lines)


# --- the loop -------------------------------------------------------------------------------------


class CalendarSync:
    """Reads the calendar periodically and feeds the schedules to the push queue."""

    def __init__(
        self,
        source: CalendarSource,
        pusher: SchedulePusher,
        clock: Clock,
        *,
        location: Callable[[], Location | None],
        relay_names: RelayNames | None = None,
        max_showers: Decimal = HARD_MAX_SHOWERS,
        interval_s: float = 300.0,
        on_report: Callable[[SyncReport], None] | None = None,
    ) -> None:
        self._source = source
        self._pusher = pusher
        self._clock = clock
        self._location = location
        self._names = relay_names
        self._max_showers = max_showers
        self._interval = interval_s
        self._on_report = on_report
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._latest: SyncReport | None = None
        self._good: CalendarSchedules | None = None
        self._auth_lost = False
        self.last_cycle: float | None = None

    def latest(self) -> SyncReport | None:
        """The report of the last cycle (``None`` before the first one)."""
        with self._lock:
            return self._latest

    def request_sync(self) -> None:
        """Ask the loop to read the calendar now (used after a UI edit)."""
        self._wake.set()

    def sync_once(self) -> SyncReport:
        """Read the calendar once, validate, push what changed, and return the report."""
        now = self._clock.now()
        try:
            schedules = read_schedules(
                self._source,
                self._clock,
                location=self._location(),
                relay_names=self._names,
                max_showers=self._max_showers,
            )
        except CalendarError as error:
            return self._failed(now, error)
        report = self._succeeded(now, schedules)
        self.last_cycle = self._clock.monotonic()
        return report

    # --- the two outcomes -----------------------------------------------------------------------

    def _failed(self, now: datetime, error: CalendarError) -> SyncReport:
        auth = isinstance(error, CalendarAuthError)
        with self._lock:
            first_time = auth != self._auth_lost
            self._auth_lost = auth
            report = SyncReport(
                at=now,
                ok=False,
                schedules=self._good,
                boiler_changed=False,
                timer_changed=False,
                diff="",
                problems=(),
                error=str(error),
                auth_lost=auth,
            )
            self._latest = report
        if first_time or not auth:
            logger.warning(kv("calendar_sync_failed", auth_lost=auth, error=error))
        self.last_cycle = self._clock.monotonic()
        self._publish(report)
        return report

    def _succeeded(self, now: datetime, schedules: CalendarSchedules) -> SyncReport:
        problems = list(schedules.problems)
        boiler_changed = timer_changed = False
        with self._lock:
            previous = self._good
        boiler_problems, boiler_file = self._push_boiler(schedules.boiler)
        timer_problems, timer_file = self._push_timer(schedules.timer)
        problems += boiler_problems + timer_problems
        if previous is None or schedules.boiler != previous.boiler:
            boiler_changed = not boiler_problems
        if previous is None or schedules.timer != previous.timer:
            timer_changed = not timer_problems
        diff = describe_changes(previous, schedules) if (boiler_changed or timer_changed) else ""
        with self._lock:
            if self._auth_lost:
                logger.info(kv("calendar_authorization_restored"))
            self._auth_lost = False
            self._good = schedules
            report = SyncReport(
                at=now,
                ok=True,
                schedules=schedules,
                boiler_changed=boiler_changed,
                timer_changed=timer_changed,
                diff=diff,
                problems=tuple(problems),
                file_changed=boiler_file or timer_file,
            )
            self._latest = report
        if boiler_changed or timer_changed:
            logger.info(kv("calendar_changed", boiler=boiler_changed, timer=timer_changed))
        self._publish(report)
        return report

    def _push_boiler(self, schedule: BoilerSchedule) -> tuple[list[str], bool]:
        """Returns the problems and whether the local file was rewritten."""
        if not schedule.slots:
            return (
                ["no boiler events in the calendar this week; the boiler schedule is unchanged"],
                False,
            )
        checked = check_boiler_schedule(schedule, self._max_showers)
        if not checked.ok:
            return [f"boiler schedule not pushed: {e}" for e in checked.errors], False
        try:
            submitted = self._pusher.set_boiler_schedule(schedule)
        except ValidationError as error:
            return [f"boiler schedule not pushed: {error}"], False
        return [], submitted.changed_locally

    def _push_timer(self, schedule: TimerSchedule) -> tuple[list[str], bool]:
        if not schedule.entries:
            return (
                ["no timer events in the calendar this week; the timer schedule is unchanged"],
                False,
            )
        checked = check_timer_schedule(schedule)
        if not checked.ok:
            return [f"timer schedule not pushed: {e}" for e in checked.errors], False
        try:
            submitted = self._pusher.set_timer_schedule(schedule)
        except ValidationError as error:
            return [f"timer schedule not pushed: {error}"], False
        return [], submitted.changed_locally

    def _publish(self, report: SyncReport) -> None:
        if self._on_report is None:
            return
        try:
            self._on_report(report)
        except Exception:  # CS-11: a failing listener must not stop the loop
            logger.exception(kv("sync_report_handler_failed"))

    # --- worker thread --------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Sync until ``stop`` is set; wakes up early on :meth:`request_sync`."""
        while not stop.is_set():
            try:
                self.sync_once()
            except Exception:  # CS-11: one bad cycle must never end the loop
                logger.exception(kv("calendar_sync_crashed"))
            self._wake.wait(self._interval)
            self._wake.clear()

    def start(self, stop: threading.Event) -> threading.Thread:
        """Start the sync loop in a daemon thread."""
        thread = threading.Thread(target=self.run, args=(stop,), name="calendar-sync", daemon=True)
        thread.start()
        return thread
