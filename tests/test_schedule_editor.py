"""Editing the schedules through the calendar (FR-CFG-4, FR-CFG-7, FR-CFG-8). Fake calendar only."""

import logging
import threading
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from orchestrator.calendar_schedules import ScheduleItem
from orchestrator.calendar_source import CalendarEvent
from orchestrator.calendar_writer import GoogleCalendarWriter
from orchestrator.event_titles import AstroTime, BoilerCommand, TimerCommand, parse_title
from orchestrator.schedule_editor import EditOutcome, EditResult, ScheduleEditor
from tests.fake_google import FakeCalendarStore, FakeHttpError
from tests.fakes import FakeClock

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=TZ)  # a Saturday
KNOWN = NOW - timedelta(days=1)  # when the calendar event was last changed, as the page knew it
SERIES = "series"
INSTANCE = "series_2026-10-19"
THREE = BoilerCommand(Decimal("3"), Decimal("2"))
FOUR = BoilerCommand(Decimal("4"), Decimal("2"))


class Rig:
    def __init__(self, *, enabled: bool = True) -> None:
        self.clock = FakeClock(NOW)
        self.store = FakeCalendarStore(self.clock.now)
        self.changed = 0
        self.editor = ScheduleEditor(
            GoogleCalendarWriter(self.store, "primary", TZ),
            self.clock,
            enabled=enabled,
            on_changed=self._count,
        )

    def _count(self) -> None:
        self.changed += 1

    def seed(
        self,
        recurrence: list[str] | None = None,
        *,
        managed: bool = True,
        summary: str = "Boiler: target=3; min=2",
        known: datetime | None = KNOWN,
    ) -> ScheduleItem:
        self.store.seed(
            SERIES,
            summary,
            "2026-10-19T06:00:00+03:00",
            recurrence=recurrence if recurrence is not None else ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"],
            managed=managed,
            updated=KNOWN,
        )
        event = CalendarEvent(
            INSTANCE,
            summary,
            datetime(2026, 10, 19, 6, 0, tzinfo=TZ),
            None,
            updated=known,
            recurring_event_id=SERIES,
        )
        return ScheduleItem(event, THREE)


@pytest.fixture
def rig() -> Rig:
    return Rig()


# --- switched off by default ------------------------------------------------------------------


def test_nothing_is_written_while_write_back_is_off() -> None:
    off = Rig(enabled=False)
    item = off.seed()
    results = [
        off.editor.create(THREE, NOW, [2], who="ui:admin"),
        off.editor.update(item, FOUR, who="ui:admin"),
        off.editor.delete(item, who="ui:admin"),
    ]
    assert all(r.outcome is EditOutcome.REFUSED for r in results)
    assert "write_back = false" in results[0].message
    assert off.store.verbs == []  # not even a read
    assert off.changed == 0


# --- create -----------------------------------------------------------------------------------


def test_create_makes_a_marked_weekly_series_starting_in_the_current_week(rig: Rig) -> None:
    result = rig.editor.create(
        BoilerCommand(Decimal("3.6"), Decimal("2")),
        NOW.replace(hour=6, minute=0),
        [2, 4],
        who="ui:admin",
    )
    assert result.ok and result.event_id == "new-1"
    assert "Monday, Wednesday" in result.message
    item = rig.store.items["new-1"]
    assert item["summary"] == "Boiler: target=3.6; min=2"
    # the week of Saturday 2026-10-24 starts on Sunday the 18th, so the series starts on Monday 19th
    assert item["start"] == {"dateTime": "2026-10-19T06:00:00+03:00", "timeZone": "Asia/Jerusalem"}
    assert item["end"]["dateTime"] == "2026-10-19T06:15:00+03:00"
    assert item["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"]
    assert item["extendedProperties"] == {"private": {"managedBy": "orchestrator"}}
    assert rig.changed == 1


def test_a_created_timer_title_reads_back_as_the_same_command(rig: Rig) -> None:
    command = TimerCommand((4,), AstroTime("sunset", -30), time(23, 0), None, None)
    result = rig.editor.create(
        command, NOW.replace(hour=12, minute=0), [1, 2, 3, 4, 5, 6, 7], who="ui:admin"
    )
    assert result.ok
    item = rig.store.items["new-1"]
    assert item["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=SU,MO,TU,WE,TH,FR,SA"]
    assert item["start"]["dateTime"] == "2026-10-18T12:00:00+03:00"  # Sunday of that week
    assert parse_title(item["summary"]).command == command


def test_a_timer_without_astro_is_created_with_the_event_end_of_the_timer(rig: Rig) -> None:
    by_end = TimerCommand((4,), None, time(23, 0), None, None)
    assert rig.editor.create(by_end, NOW.replace(hour=17, minute=30), [2], who="ui:admin").ok
    assert rig.store.items["new-1"]["end"]["dateTime"].startswith("2026-10-19T23:00:00")
    by_duration = TimerCommand((4,), None, None, None, 5400)
    assert rig.editor.create(by_duration, NOW.replace(hour=22, minute=0), [2], who="ui:admin").ok
    assert rig.store.items["new-2"]["end"]["dateTime"].startswith("2026-10-19T23:30:00")


def test_a_timer_ending_before_its_start_is_created_ending_on_the_next_day(rig: Rig) -> None:
    command = TimerCommand((4,), None, time(6, 0), None, None)
    assert rig.editor.create(command, NOW.replace(hour=22, minute=0), [7], who="ui:admin").ok
    assert rig.store.items["new-1"]["end"]["dateTime"].startswith("2026-10-25T06:00:00")


@pytest.mark.parametrize(
    ("command", "days", "message"),
    [
        (BoilerCommand(Decimal("6"), Decimal("2")), [2], "above the limit of 5"),
        (BoilerCommand(Decimal("3"), Decimal("-1")), [2], "below 0"),
        (TimerCommand((4,), None, None, AstroTime("sunrise"), None), [2], "needs an astro start"),
        (THREE, [], "at least one day"),
        (THREE, [8], "day of week"),
    ],
)
def test_create_refuses_what_is_not_acceptable_and_writes_nothing(
    rig: Rig, command: BoilerCommand | TimerCommand, days: list[int], message: str
) -> None:
    result = rig.editor.create(command, NOW, days, who="ui:admin")
    assert result.outcome is EditOutcome.REFUSED and message in result.message
    assert rig.store.verbs == [] and rig.changed == 0


# --- update -----------------------------------------------------------------------------------


def test_update_changes_the_title_of_the_whole_series_and_nothing_else(rig: Rig) -> None:
    item = rig.seed()
    result = rig.editor.update(item, FOUR, who="ui:admin")
    assert (
        result.ok
        and "Changed 'Boiler: target=3; min=2' to 'Boiler: target=4; min=2'" in result.message
    )
    stored = rig.store.items[SERIES]
    assert stored["summary"] == "Boiler: target=4; min=2"
    assert stored["start"]["dateTime"] == "2026-10-19T06:00:00+03:00"  # untouched
    assert stored["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"]  # untouched
    assert rig.store.verbs == [("get", SERIES), ("patch", SERIES)]  # the series, not the instance
    assert rig.changed == 1


def test_update_can_move_the_time_of_day(rig: Rig) -> None:
    item = rig.seed()
    assert rig.editor.update(item, THREE, who="ui:admin", start_time=time(7, 30)).ok
    stored = rig.store.items[SERIES]
    assert stored["start"]["dateTime"] == "2026-10-19T07:30:00+03:00"
    assert stored["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"]


def test_update_can_change_the_days_of_our_own_events(rig: Rig) -> None:
    item = rig.seed()
    assert rig.editor.update(item, THREE, who="ui:admin", days=[3, 5]).ok
    stored = rig.store.items[SERIES]
    assert stored["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH"]
    assert stored["start"]["dateTime"] == "2026-10-20T06:00:00+03:00"  # first Tuesday of that week


def test_the_days_of_a_hand_made_event_cannot_be_changed_from_here(rig: Rig) -> None:
    item = rig.seed(["RRULE:FREQ=WEEKLY;BYDAY=MO"], managed=False)
    result = rig.editor.update(item, THREE, who="ui:admin", days=[3])
    assert result.outcome is EditOutcome.REFUSED and "can only be changed there" in result.message
    assert rig.store.writes() == []


def test_a_bad_value_is_refused_before_anything_is_written(rig: Rig) -> None:
    item = rig.seed()
    result = rig.editor.update(item, BoilerCommand(Decimal("5.5"), Decimal("2")), who="ui:admin")
    assert result.outcome is EditOutcome.REFUSED and "above the limit of 5" in result.message
    assert rig.store.verbs == []


# --- which hand-made events may be edited -----------------------------------------------------


@pytest.mark.parametrize(
    "recurrence",
    [
        [],  # a single event
        ["RRULE:FREQ=WEEKLY;BYDAY=MO"],  # a weekly series on one weekday
        ["RRULE:FREQ=WEEKLY"],  # weekly, weekday taken from the start
        ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=20"],
        [
            "RRULE:FREQ=WEEKLY;BYDAY=MO",
            "EXDATE;TZID=Asia/Jerusalem:20261207T060000",
        ],  # one day skipped
    ],
)
def test_plain_and_single_weekday_events_made_by_hand_can_be_edited(
    rig: Rig, recurrence: list[str]
) -> None:
    item = rig.seed(recurrence, managed=False)
    assert rig.editor.update(item, FOUR, who="ui:admin").ok
    assert rig.store.items[SERIES]["summary"] == "Boiler: target=4; min=2"


@pytest.mark.parametrize("recurrence", [[], ["RRULE:FREQ=WEEKLY;BYDAY=MO"]])
def test_plain_and_single_weekday_events_made_by_hand_can_be_deleted(
    rig: Rig, recurrence: list[str]
) -> None:
    item = rig.seed(recurrence, managed=False)
    assert rig.editor.delete(item, who="ui:admin").ok
    assert SERIES not in rig.store.items


def test_a_second_edit_from_a_stale_page_is_a_conflict_until_the_page_is_reloaded(rig: Rig) -> None:
    item = rig.seed()
    assert rig.editor.update(item, FOUR, who="ui:admin").ok  # this stamps the event as changed now
    stale = rig.editor.update(item, THREE, who="ui:admin")  # the page still shows the old stamp
    assert stale.outcome is EditOutcome.CONFLICT
    assert rig.store.items[SERIES]["summary"] == "Boiler: target=4; min=2"


@pytest.mark.parametrize(
    ("recurrence", "reason"),
    [
        (["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"], "a series on several weekdays"),
        (["RRULE:FREQ=DAILY"], "not a weekly series"),
        (["RRULE:FREQ=MONTHLY;BYDAY=1MO"], "not a weekly series"),
        (["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"], "does not repeat every week"),
        (["RRULE:FREQ=WEEKLY;BYDAY=MO", "RDATE:20261201T060000Z"], "a series with extra dates"),
    ],
)
def test_series_that_could_change_days_the_user_did_not_touch_are_refused(
    rig: Rig, recurrence: list[str], reason: str
) -> None:
    item = rig.seed(recurrence, managed=False)
    for result in (
        rig.editor.update(item, FOUR, who="ui:admin"),
        rig.editor.delete(item, who="ui:admin"),
    ):
        assert result.outcome is EditOutcome.REFUSED
        assert reason in result.message and "edit it in Google Calendar" in result.message
    assert rig.store.writes() == []
    assert rig.store.items[SERIES]["summary"] == "Boiler: target=3; min=2"


def test_an_all_day_event_is_never_edited(rig: Rig) -> None:
    rig.store.seed(SERIES, "Boiler: target=3; min=2", "2026-10-19", all_day=True, updated=KNOWN)
    event = CalendarEvent(SERIES, "Boiler: target=3; min=2", NOW, None, updated=KNOWN)
    result = rig.editor.update(ScheduleItem(event, THREE), FOUR, who="ui:admin")
    assert result.outcome is EditOutcome.REFUSED and "all-day" in result.message
    assert rig.store.writes() == []


# --- conflicts: the latest modification wins (FR-CFG-7) ---------------------------------------


def test_no_conflict_when_the_calendar_event_is_unchanged_since_the_page_loaded(rig: Rig) -> None:
    item = rig.seed()
    assert rig.editor.update(item, FOUR, who="ui:admin").ok


def test_the_calendar_wins_when_it_changed_after_the_edit_was_made(rig: Rig) -> None:
    item = rig.seed()
    rig.store.touch(SERIES, NOW + timedelta(minutes=2))  # someone edited Google Calendar later
    result = rig.editor.update(item, FOUR, who="ui:admin", edited_at=NOW)
    assert result.outcome is EditOutcome.CONFLICT and "reload" in result.message
    assert rig.store.writes() == []
    assert rig.changed == 0
    delete = rig.editor.delete(item, who="ui:admin", edited_at=NOW)
    assert delete.outcome is EditOutcome.CONFLICT and SERIES in rig.store.items


def test_a_tie_goes_to_the_calendar(rig: Rig) -> None:
    item = rig.seed()
    rig.store.touch(SERIES, NOW)
    assert (
        rig.editor.update(item, FOUR, who="ui:admin", edited_at=NOW).outcome is EditOutcome.CONFLICT
    )


def test_the_edit_wins_when_it_is_newer_than_the_calendar_change_and_both_are_logged(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    item = rig.seed()
    rig.store.touch(
        SERIES, NOW - timedelta(hours=1)
    )  # changed after the page loaded, before the edit
    result = rig.editor.update(item, FOUR, who="ui:admin")
    assert result.ok
    assert rig.store.items[SERIES]["summary"] == "Boiler: target=4; min=2"
    lines = [r.getMessage() for r in caplog.records]
    assert any("event=schedule_edit_conflict" in line and "winner=ui" in line for line in lines)
    assert any("event=schedule_conflict" in line and "winner=ui" in line for line in lines)


def test_an_item_without_a_known_change_time_is_not_checked_for_conflicts(rig: Rig) -> None:
    item = rig.seed(known=None)
    rig.store.touch(SERIES, NOW + timedelta(hours=1))
    assert rig.editor.update(item, FOUR, who="ui:admin").ok


# --- delete -----------------------------------------------------------------------------------


def test_delete_removes_the_whole_series(rig: Rig) -> None:
    item = rig.seed()
    result = rig.editor.delete(item, who="ui:admin")
    assert result.ok and "Deleted 'Boiler: target=3; min=2'" in result.message
    assert SERIES not in rig.store.items
    assert rig.store.verbs == [("get", SERIES), ("delete", SERIES)]
    assert rig.changed == 1


def test_deleting_something_that_is_already_gone_is_reported_not_crashed(rig: Rig) -> None:
    item = rig.seed()
    del rig.store.items[SERIES]
    result = rig.editor.delete(item, who="ui:admin")
    assert result.outcome is EditOutcome.FAILED and "could not be changed" in result.message


# --- calendar trouble -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FakeHttpError(401), "Google authorization is needed"),
        (FakeHttpError(403, "forbidden"), "Google authorization is needed"),
        (FakeHttpError(503), "could not be changed"),
        (TimeoutError("slow"), "could not be changed"),
    ],
)
def test_calendar_trouble_is_reported_and_nothing_changes(
    rig: Rig, error: Exception, message: str
) -> None:
    item = rig.seed()
    rig.store.error = error
    for result in (
        rig.editor.update(item, FOUR, who="ui:admin"),
        rig.editor.create(THREE, NOW, [2], who="ui:admin"),
        rig.editor.delete(item, who="ui:admin"),
    ):
        assert result.outcome is EditOutcome.FAILED and message in result.message
    assert rig.changed == 0


# --- audit trail ------------------------------------------------------------------------------


def test_every_attempt_is_audited_with_who_action_event_and_result(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    item = rig.seed(["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"], managed=False)
    created = rig.editor.create(THREE, NOW, [2], who="ui:admin")
    refused = rig.editor.update(item, FOUR, who="ui:guest")
    lines = [r.getMessage() for r in caplog.records if r.name == "orchestrator.audit"]
    assert len(lines) == 2
    assert (
        "event=schedule_edit who=ui:admin channel=ui action=create calendar_event=new-1 "
        "result=applied" in lines[0]
    )
    assert "who=ui:guest channel=ui action=update calendar_event=series_2026-10-19" in lines[1]
    assert "result=refused detail=" in lines[1] and "several weekdays" in lines[1]
    assert created.correlation_id and created.correlation_id != refused.correlation_id


def test_edits_from_several_threads_never_interleave(rig: Rig) -> None:
    results: list[EditResult] = []

    def make(day: int) -> None:
        results.append(rig.editor.create(THREE, NOW, [day], who="ui:admin"))

    threads = [threading.Thread(target=make, args=(d,)) for d in range(1, 8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 7 and all(r.ok for r in results)
    assert len({r.event_id for r in results}) == 7
    assert rig.changed == 7
