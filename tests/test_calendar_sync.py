"""The calendar sync loop and the whole UI -> calendar -> schedule path (FR-CFG-1/4/7)."""

import logging
import threading
import time as wall_time
from collections.abc import Callable
from datetime import datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orchestrator.astro import Location
from orchestrator.calendar_schedules import CalendarSchedules
from orchestrator.calendar_source import GoogleCalendarSource
from orchestrator.calendar_sync import CalendarSync, SyncReport, describe_changes
from orchestrator.calendar_writer import GoogleCalendarWriter
from orchestrator.event_titles import BoilerCommand
from orchestrator.models import BoilerSchedule, BoilerSlot, TimerSchedule
from orchestrator.push_queue import SubmitResult
from orchestrator.schedule_editor import ScheduleEditor
from orchestrator.validation import ValidationError
from tests.fake_google import FakeCalendarService, FakeCalendarStore, FakeHttpError, google_event
from tests.fakes import FakeClock

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=TZ)  # a Saturday; the week starts on 2026-10-18
LOCATION = Location(32.07, 34.82)
BOILER_EVENT = google_event("Boiler: target=3.6; min=2", "2026-10-19T06:00:00+03:00", id="b1")
TIMER_EVENT = google_event("Timer: relaynum 4 end 23:00", "2026-10-19T17:30:00+03:00", id="t1")


class FakePusher:
    def __init__(self) -> None:
        self.boiler: list[BoilerSchedule] = []
        self.timer: list[TimerSchedule] = []
        self.reject = False

    def set_boiler_schedule(self, schedule: BoilerSchedule) -> SubmitResult:
        if self.reject:
            raise ValidationError(("not acceptable",))
        self.boiler.append(schedule)
        return SubmitResult(changed_locally=True, pending=True)

    def set_timer_schedule(self, schedule: TimerSchedule) -> SubmitResult:
        if self.reject:
            raise ValidationError(("not acceptable",))
        self.timer.append(schedule)
        return SubmitResult(changed_locally=True, pending=True)


def make_sync(
    service: Any,
    pusher: FakePusher | None = None,
    *,
    location: Location | None = LOCATION,
    on_report: Callable[[SyncReport], None] | None = None,
    interval_s: float = 300.0,
) -> tuple[CalendarSync, FakePusher, FakeClock]:
    clock = FakeClock(NOW)
    push = pusher or FakePusher()
    sync = CalendarSync(
        GoogleCalendarSource(service, "primary", TZ),
        push,
        clock,
        location=lambda: location,
        on_report=on_report,
        interval_s=interval_s,
    )
    return sync, push, clock


def page(*events: dict[str, Any]) -> FakeCalendarService:
    return FakeCalendarService([{"items": list(events)}], stable=True)


# --- one cycle --------------------------------------------------------------------------------


def test_the_first_cycle_pushes_both_schedules_and_describes_what_was_added() -> None:
    sync, pusher, _ = make_sync(page(BOILER_EVENT, TIMER_EVENT))
    report = sync.sync_once()
    assert report.ok and report.boiler_changed and report.timer_changed
    assert len(pusher.boiler) == 1 and len(pusher.timer) == 1
    lines = report.diff.split("\r\n")
    assert lines[0] == "The following dates were added to the calendar:"
    assert "  boiler Monday 06:00:00 target 3.6 min 2" in lines
    assert "  timer relay 4 on Monday 17:30:00" in lines
    assert "  timer relay 4 off Monday 23:00:00" in lines
    assert report.problems == () and report.error is None and not report.auth_lost
    assert sync.latest() is report


def test_an_unchanged_calendar_reports_no_change() -> None:
    sync, pusher, _ = make_sync(page(BOILER_EVENT, TIMER_EVENT))
    sync.sync_once()
    again = sync.sync_once()
    assert again.ok and not again.boiler_changed and not again.timer_changed and again.diff == ""
    assert len(pusher.boiler) == 2  # offered again; the push queue ignores identical content


def test_a_changed_event_is_reported_as_one_removal_and_one_addition() -> None:
    service = FakeCalendarService(
        [
            {"items": [BOILER_EVENT, TIMER_EVENT]},
            {
                "items": [
                    google_event("Boiler: target=4; min=2", "2026-10-19T06:00:00+03:00", id="b1"),
                    TIMER_EVENT,
                ]
            },
        ]
    )
    sync, _, _ = make_sync(service)
    sync.sync_once()
    report = sync.sync_once()
    assert report.boiler_changed and not report.timer_changed
    assert "added to the calendar:\r\n  boiler Monday 06:00:00 target 4 min 2" in report.diff
    assert "removed from the calendar:\r\n  boiler Monday 06:00:00 target 3.6 min 2" in report.diff


def test_describe_changes_handles_the_very_first_reading_and_no_change() -> None:
    sync, _, _ = make_sync(page(BOILER_EVENT))
    report = sync.sync_once()
    assert report.schedules is not None
    assert describe_changes(report.schedules, report.schedules) == ""
    assert "removed" not in describe_changes(None, report.schedules)


# --- calendar trouble: the last good schedules stay in force ----------------------------------


def test_when_the_calendar_cannot_be_read_nothing_changes_and_the_reason_is_reported() -> None:
    service = FakeCalendarService([{"items": [BOILER_EVENT, TIMER_EVENT]}])
    sync, pusher, _ = make_sync(service)
    good = sync.sync_once()
    service.error = FakeHttpError(503)
    bad = sync.sync_once()
    assert not bad.ok and not bad.auth_lost and "HTTP 503" in (bad.error or "")
    assert bad.schedules == good.schedules  # the last good reading is still shown
    assert not bad.boiler_changed and bad.diff == ""
    assert len(pusher.boiler) == 1  # nothing was pushed in the failing cycle


def test_a_lost_authorization_is_flagged_logged_once_and_cleared_on_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    service = FakeCalendarService([{"items": [BOILER_EVENT, TIMER_EVENT]}])
    sync, _, _ = make_sync(service)
    service.error = FakeHttpError(401)
    first = sync.sync_once()
    second = sync.sync_once()
    assert first.auth_lost and second.auth_lost and first.schedules is None
    assert caplog.text.count("event=calendar_sync_failed") == 1  # not repeated every cycle
    service.error = None
    recovered = sync.sync_once()
    assert recovered.ok and not recovered.auth_lost
    assert "event=calendar_authorization_restored" in caplog.text


def test_the_error_text_never_contains_the_response_body() -> None:
    service = FakeCalendarService()
    error = FakeHttpError(403, "forbidden")
    error.content = b"SECRET-BODY-4711"
    service.error = error
    sync, _, _ = make_sync(service)
    assert "SECRET-BODY-4711" not in (sync.sync_once().error or "")


# --- schedules that must not be pushed --------------------------------------------------------


def test_a_week_without_boiler_events_does_not_erase_the_boiler_schedule() -> None:
    sync, pusher, _ = make_sync(page(TIMER_EVENT))
    report = sync.sync_once()
    assert pusher.boiler == [] and len(pusher.timer) == 1
    assert not report.boiler_changed and report.timer_changed
    assert any("no boiler events" in p for p in report.problems)


def test_a_week_without_timer_events_does_not_erase_the_timer_schedule() -> None:
    sync, pusher, _ = make_sync(page(BOILER_EVENT))
    report = sync.sync_once()
    assert pusher.timer == [] and len(pusher.boiler) == 1
    assert any("no timer events" in p for p in report.problems)


def test_an_event_above_the_limit_is_skipped_and_the_rest_is_still_pushed() -> None:
    too_hot = google_event("Boiler: target=6; min=2", "2026-10-20T06:00:00+03:00", id="hot")
    sync, pusher, _ = make_sync(page(BOILER_EVENT, too_hot, TIMER_EVENT))
    report = sync.sync_once()
    assert [s.day for s in pusher.boiler[0].slots] == [2]  # only the Monday slot
    assert any("above the limit of 5" in p for p in report.problems)


def test_a_schedule_the_push_queue_refuses_is_reported_not_marked_as_changed() -> None:
    pusher = FakePusher()
    pusher.reject = True
    sync, _, _ = make_sync(page(BOILER_EVENT, TIMER_EVENT), pusher)
    report = sync.sync_once()
    assert not report.boiler_changed and not report.timer_changed and report.diff == ""
    assert any("boiler schedule not pushed" in p for p in report.problems)
    assert any("timer schedule not pushed" in p for p in report.problems)


def test_sun_events_without_a_location_are_reported_every_time() -> None:
    sun = google_event(
        "Timer: relaynum 4 start sunset end 23:00", "2026-10-19T12:00:00+03:00", id="s"
    )
    sync, _, _ = make_sync(page(BOILER_EVENT, sun), location=None)
    report = sync.sync_once()
    assert any("no location is configured" in p for p in report.problems)


# --- listeners and the loop -------------------------------------------------------------------


def test_every_report_is_published_and_a_failing_listener_is_harmless(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[SyncReport] = []

    def listener(report: SyncReport) -> None:
        seen.append(report)
        raise RuntimeError("listener bug")

    sync, _, clock = make_sync(page(BOILER_EVENT, TIMER_EVENT), on_report=listener)
    sync.sync_once()
    sync.sync_once()
    assert len(seen) == 2
    assert "sync_report_handler_failed" in caplog.text
    assert sync.last_cycle == clock.monotonic()


def wait_until(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    deadline = wall_time.monotonic() + seconds
    while wall_time.monotonic() < deadline:
        if condition():
            return True
        wall_time.sleep(0.01)
    return condition()


def test_the_loop_syncs_at_once_on_request_and_stops_cleanly() -> None:
    service = FakeCalendarService([{"items": [BOILER_EVENT, TIMER_EVENT]}])
    sync, _, _ = make_sync(service, interval_s=60.0)  # the interval is far too long to matter
    stop = threading.Event()
    thread = sync.start(stop)
    try:
        assert wait_until(lambda: len(service.calls) >= 1)
        sync.request_sync()  # as after a UI edit
        assert wait_until(lambda: len(service.calls) >= 2)
    finally:
        stop.set()
        sync.request_sync()
        thread.join(timeout=3)
    assert not thread.is_alive() and thread.daemon and thread.name == "calendar-sync"


def test_the_loop_survives_an_unexpected_error(caplog: pytest.LogCaptureFixture) -> None:
    class Flaky:
        calls = 0

        def read(self, window_start: datetime, window_end: datetime) -> Any:
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise ValueError("unexpected bug")
            return GoogleCalendarSource(page(BOILER_EVENT, TIMER_EVENT), "primary", TZ).read(
                window_start, window_end
            )

    clock = FakeClock(NOW)
    sync = CalendarSync(Flaky(), FakePusher(), clock, location=lambda: LOCATION, interval_s=0.01)
    stop = threading.Event()
    thread = sync.start(stop)
    try:
        assert wait_until(lambda: sync.latest() is not None)
    finally:
        stop.set()
        sync.request_sync()
        thread.join(timeout=3)
    assert "calendar_sync_crashed" in caplog.text
    latest = sync.latest()
    assert latest is not None and latest.ok


# --- the whole path: UI edit -> calendar -> next sync -> schedule -----------------------------


class World:
    """A fake calendar, an editor writing to it, and a sync reading from it."""

    def __init__(self) -> None:
        self.clock = FakeClock(NOW)
        self.store = FakeCalendarStore(self.clock.now)
        self.pusher = FakePusher()
        self.sync = CalendarSync(
            GoogleCalendarSource(self.store, "primary", TZ),
            self.pusher,
            self.clock,
            location=lambda: LOCATION,
        )
        self.editor = ScheduleEditor(
            GoogleCalendarWriter(self.store, "primary", TZ),
            self.clock,
            enabled=True,
            on_changed=self.sync.request_sync,
        )

    def slots(self) -> tuple[BoilerSlot, ...]:
        report = self.sync.sync_once()
        assert report.schedules is not None
        return report.schedules.boiler.slots

    def schedules(self) -> CalendarSchedules:
        report = self.sync.latest()
        assert report is not None and report.schedules is not None
        return report.schedules


def test_a_ui_edit_shows_up_in_the_schedule_after_the_next_sync() -> None:
    world = World()
    created = world.editor.create(
        BoilerCommand(Decimal("3.6"), Decimal("2")),
        NOW.replace(hour=6, minute=0),
        [2, 4],
        who="ui:admin",
    )
    assert created.ok
    assert world.slots() == (
        BoilerSlot(2, time(6, 0), Decimal("3.6"), Decimal("2")),
        BoilerSlot(4, time(6, 0), Decimal("3.6"), Decimal("2")),
    )
    assert world.pusher.boiler[-1].slots == world.schedules().boiler.slots


def test_a_change_made_in_google_calendar_shows_up_in_the_schedule_too() -> None:
    world = World()
    world.editor.create(
        BoilerCommand(Decimal("3"), Decimal("2")),
        NOW.replace(hour=6, minute=0),
        [2],
        who="ui:admin",
    )
    world.store.seed("by-hand", "Boiler: target=4; min=3", "2026-10-21T18:00:00+03:00", updated=NOW)
    assert [(s.day, s.target) for s in world.slots()] == [(2, Decimal("3")), (4, Decimal("4"))]


def test_editing_and_deleting_through_the_ui_changes_the_schedule_again() -> None:
    world = World()
    world.editor.create(
        BoilerCommand(Decimal("3"), Decimal("2")),
        NOW.replace(hour=6, minute=0),
        [2],
        who="ui:admin",
    )
    world.editor.create(
        BoilerCommand(Decimal("3"), Decimal("2")),
        NOW.replace(hour=18, minute=0),
        [2],
        who="ui:admin",
    )
    world.clock.advance(world.clock.now().replace(minute=50) - world.clock.now())  # time passes
    world.slots()
    morning = next(i for i in world.schedules().items if i.event.start.hour == 6)
    assert world.editor.update(
        morning, BoilerCommand(Decimal("4.5"), Decimal("2")), who="ui:admin"
    ).ok
    assert [(s.start.hour, s.target) for s in world.slots()] == [
        (6, Decimal("4.5")),
        (18, Decimal("3")),
    ]
    evening = next(i for i in world.schedules().items if i.event.start.hour == 18)
    assert world.editor.delete(evening, who="ui:admin").ok
    assert [s.start.hour for s in world.slots()] == [6]
