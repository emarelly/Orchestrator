"""The calendar writer: bodies sent to Google, parsing what comes back, errors (FR-CFG-4, CS-9)."""

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orchestrator.calendar_source import CalendarAuthError, CalendarError, CalendarUnavailable
from orchestrator.calendar_writer import GoogleCalendarWriter
from tests.fake_google import FakeCalendarStore, FakeHttpError

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=TZ)


def writer_and_store() -> tuple[GoogleCalendarWriter, FakeCalendarStore]:
    store = FakeCalendarStore(lambda: NOW)
    return GoogleCalendarWriter(store, "family@example.com", TZ), store


def test_create_sends_the_expected_body_and_marks_the_event() -> None:
    writer, store = writer_and_store()
    created = writer.create(
        summary="Boiler: target=3; min=2",
        start=datetime(2026, 10, 19, 6, 0, tzinfo=TZ),
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    )
    assert created.managed and created.event_id == "new-1"
    assert created.start == datetime(2026, 10, 19, 6, 0, tzinfo=TZ)
    assert created.recurrence == ("RRULE:FREQ=WEEKLY;BYDAY=MO",)
    assert created.updated == NOW
    body: dict[str, Any] = store.items["new-1"]
    assert body["end"]["dateTime"] == "2026-10-19T06:15:00+03:00"
    assert body["end"]["timeZone"] == "Asia/Jerusalem"


def test_a_single_event_needs_no_series_and_no_zone_name() -> None:
    store = FakeCalendarStore(lambda: NOW)
    fixed = GoogleCalendarWriter(store, "primary", timezone(timedelta(hours=3)))
    created = fixed.create(summary="Boiler: target=3; min=2", start=NOW)
    assert "recurrence" not in store.items[created.event_id]
    assert "timeZone" not in store.items[created.event_id]["start"]


def test_a_series_needs_a_named_time_zone() -> None:
    store = FakeCalendarStore(lambda: NOW)
    fixed = GoogleCalendarWriter(store, "primary", timezone(timedelta(hours=3)))
    with pytest.raises(CalendarError, match="named time zone"):
        fixed.create(summary="x", start=NOW, recurrence=["RRULE:FREQ=WEEKLY"])
    assert store.verbs == []


def test_get_reads_the_fields_the_editor_needs() -> None:
    writer, store = writer_and_store()
    store.seed(
        "e1", "Dentist", "2026-10-20T10:00:00Z", recurrence=["RRULE:FREQ=WEEKLY"], updated=NOW
    )
    remote = writer.get("e1")
    assert remote.summary == "Dentist" and not remote.managed and not remote.all_day
    assert remote.start == datetime(2026, 10, 20, 13, 0, tzinfo=TZ)  # 10:00 UTC in summer time
    assert remote.recurrence == ("RRULE:FREQ=WEEKLY",)
    assert remote.updated == NOW


def test_get_marks_all_day_events_and_survives_odd_data() -> None:
    writer, store = writer_and_store()
    store.seed("day", "Holiday", "2026-10-20", all_day=True)
    assert writer.get("day").all_day
    store.items["odd"] = {"id": "odd", "start": {"dateTime": "garbage"}, "updated": "also garbage"}
    odd = writer.get("odd")
    assert odd.start is None and odd.updated is None and odd.summary == ""


def test_patch_sends_only_the_fields_that_change() -> None:
    writer, store = writer_and_store()
    store.seed("e1", "Boiler: target=3; min=2", "2026-10-19T06:00:00+03:00", managed=True)
    writer.patch("e1", summary="Boiler: target=4; min=2")
    assert store.items["e1"]["summary"] == "Boiler: target=4; min=2"
    assert store.items["e1"]["start"] == {"dateTime": "2026-10-19T06:00:00+03:00"}  # untouched


def test_delete_treats_an_event_that_is_already_gone_as_deleted() -> None:
    writer, store = writer_and_store()
    store.seed("e1", "x", "2026-10-19T06:00:00+03:00")
    writer.delete("e1")
    writer.delete("e1")  # a second delete: 404 from the fake, still fine
    store.error = FakeHttpError(410)
    writer.delete("e1")
    assert "e1" not in store.items


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeHttpError(401), CalendarAuthError),
        (FakeHttpError(403, "forbidden"), CalendarAuthError),
        (FakeHttpError(500), CalendarUnavailable),
        (TimeoutError("slow"), CalendarUnavailable),
    ],
)
def test_errors_are_sorted_like_the_reader_does(
    error: Exception, expected: type[CalendarError]
) -> None:
    writer, store = writer_and_store()
    store.error = error
    with pytest.raises(CalendarError) as caught:
        writer.create(summary="x", start=NOW)
    assert type(caught.value) is expected
    with pytest.raises(CalendarError):
        writer.delete("anything")


def test_an_unexpected_error_is_a_bug_and_is_not_disguised() -> None:
    writer, store = writer_and_store()
    store.error = ZeroDivisionError("bug")
    with pytest.raises(ZeroDivisionError):
        writer.get("x")
