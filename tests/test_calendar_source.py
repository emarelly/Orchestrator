"""Reading events from Google Calendar through a fake service (FR-CFG-1, FR-GOOG-4, CS-9)."""

import importlib
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from google.auth.exceptions import RefreshError, TransportError

from orchestrator.calendar_source import (
    MAX_PAGES,
    CalendarAuthError,
    CalendarError,
    CalendarUnavailable,
    GoogleCalendarSource,
    build_calendar_service,
    week_window,
)
from orchestrator.weekdays import week_start
from tests.fake_google import FakeCalendarService, FakeHttpError, google_event

JERUSALEM = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=JERUSALEM)  # a Saturday


def google_error(kind: Any, message: str) -> Exception:
    """Build a google-auth exception (its constructor is untyped, so it is called through Any)."""
    error: Exception = kind(message)
    return error


def source(service: FakeCalendarService) -> GoogleCalendarSource:
    return GoogleCalendarSource(service, "family@example.com", JERUSALEM)


def read(service: FakeCalendarService) -> Any:
    start, end = week_window(NOW)
    return source(service).read(start, end)


# --- the week window --------------------------------------------------------------------------


def test_the_window_is_sunday_to_the_next_sunday_in_the_local_zone() -> None:
    start, end = week_window(NOW)
    assert start == datetime(2026, 10, 18, 0, 0, tzinfo=JERUSALEM)
    assert end == datetime(2026, 10, 25, 0, 0, tzinfo=JERUSALEM)
    assert start.isoformat() == "2026-10-18T00:00:00+03:00"
    assert end.isoformat() == "2026-10-25T00:00:00+03:00"  # the clocks change at 02:00, not 00:00


def test_a_sunday_starts_its_own_week_and_the_window_follows_the_clock_change() -> None:
    start, end = week_window(datetime(2026, 10, 25, 9, 0, tzinfo=JERUSALEM))
    assert start.isoformat() == "2026-10-25T00:00:00+03:00"
    assert end.isoformat() == "2026-11-01T00:00:00+02:00"  # winter time by the end of the week


def test_week_start_is_the_sunday_on_or_before() -> None:
    assert week_start(datetime(2026, 10, 24).date()).isoformat() == "2026-10-18"  # Saturday
    assert week_start(datetime(2026, 10, 25).date()).isoformat() == "2026-10-25"  # Sunday
    assert week_start(datetime(2026, 10, 26).date()).isoformat() == "2026-10-25"  # Monday


def test_a_naive_time_has_no_window() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        week_window(datetime(2026, 10, 24, 16, 41))


# --- what is asked from Google ----------------------------------------------------------------


def test_the_request_expands_recurring_events_and_asks_for_the_right_window() -> None:
    service = FakeCalendarService()
    read(service)
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["calendarId"] == "family@example.com"
    assert call["singleEvents"] is True  # recurring events become one event per day
    assert call["orderBy"] == "startTime"
    assert call["timeMin"] == "2026-10-18T00:00:00+03:00"
    assert call["timeMax"] == "2026-10-25T00:00:00+03:00"
    assert service.retries == [2]  # transient 5xx errors are retried by the client library


# --- converting events ------------------------------------------------------------------------


def test_events_are_converted_to_the_local_zone() -> None:
    page = {
        "items": [
            google_event(
                "Boiler: target=3.6; min=2",
                "2026-10-20T06:00:00+03:00",
                "2026-10-20T07:00:00+03:00",
                id="a",
                recurringEventId="series",
            ),
            google_event("Boiler: target=3; min=2", "2026-10-20T10:00:00Z", id="b"),  # UTC input
        ]
    }
    result = read(FakeCalendarService([page]))
    first, second = result.events
    assert first.event_id == "a" and first.recurring_event_id == "series"
    assert first.start == datetime(2026, 10, 20, 6, 0, tzinfo=JERUSALEM)
    assert first.end == datetime(2026, 10, 20, 7, 0, tzinfo=JERUSALEM)
    assert not first.all_day
    assert second.start.hour == 13  # 10:00 UTC is 13:00 in summer time (+03:00)
    assert second.start.tzinfo is JERUSALEM


def test_an_all_day_event_is_marked_and_starts_at_midnight() -> None:
    page = {
        "items": [google_event("Timer: relaynum 4 duration 60", "2026-10-20", all_day=True, id="d")]
    }
    (event,) = read(FakeCalendarService([page])).events
    assert event.all_day and event.start.time() == time(0, 0)


def test_cancelled_events_and_events_without_a_title_are_ignored() -> None:
    page = {
        "items": [
            google_event(
                "Boiler: target=3; min=2", "2026-10-20T06:00:00+03:00", status="cancelled", id="c"
            ),
            google_event(None, "2026-10-20T07:00:00+03:00", id="n"),
            google_event("   ", "2026-10-20T08:00:00+03:00", id="s"),
            google_event("Dentist", "2026-10-20T09:00:00+03:00", id="ok"),
        ]
    }
    result = read(FakeCalendarService([page]))
    assert [e.event_id for e in result.events] == ["ok"]
    assert result.skipped == ()


def test_an_event_with_an_unreadable_time_is_skipped_with_a_note() -> None:
    page = {
        "items": [
            google_event("Boiler: target=3; min=2", "not-a-time", id="bad"),
            {"id": "x", "summary": "no start"},
        ]
    }
    result = read(FakeCalendarService([page]))
    assert result.events == ()
    assert result.skipped == (
        "event bad: unreadable start or end time",
        "event x: unreadable start or end time",
    )


def test_an_answer_without_items_is_an_empty_week() -> None:
    assert read(FakeCalendarService([{}])).events == ()


def test_several_pages_are_combined() -> None:
    pages: list[dict[str, Any]] = [
        {"items": [google_event("A", "2026-10-20T06:00:00+03:00", id="1")], "nextPageToken": "t1"},
        {"items": [google_event("B", "2026-10-20T07:00:00+03:00", id="2")], "nextPageToken": "t2"},
        {"items": [google_event("C", "2026-10-20T08:00:00+03:00", id="3")]},
    ]
    service = FakeCalendarService(pages)
    result = read(service)
    assert [e.event_id for e in result.events] == ["1", "2", "3"]
    assert [call["pageToken"] for call in service.calls] == [None, "t1", "t2"]


def test_endless_paging_is_cut_off_and_reported() -> None:
    pages: list[dict[str, Any]] = [
        {"items": [], "nextPageToken": f"t{i}"} for i in range(MAX_PAGES + 5)
    ]
    service = FakeCalendarService(pages)
    result = read(service)
    assert len(service.calls) == MAX_PAGES
    assert any("more than" in note for note in result.skipped)


# --- errors (CS-9, FR-GOOG-7) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (google_error(RefreshError, "invalid_grant"), CalendarAuthError),
        (FakeHttpError(401), CalendarAuthError),
        (FakeHttpError(403, "forbidden"), CalendarAuthError),
        (FakeHttpError(403, "rateLimitExceeded"), CalendarUnavailable),
        (FakeHttpError(403, "userRateLimitExceeded"), CalendarUnavailable),
        (FakeHttpError(429), CalendarUnavailable),
        (FakeHttpError(500), CalendarUnavailable),
        (FakeHttpError(503), CalendarUnavailable),
        (google_error(TransportError, "no route"), CalendarUnavailable),
        (TimeoutError("slow"), CalendarUnavailable),
        (ConnectionResetError("reset"), CalendarUnavailable),
        (FakeHttpError(404), CalendarError),
    ],
)
def test_google_and_network_errors_are_sorted_into_the_cases_the_caller_needs(
    error: Exception, expected: type[CalendarError]
) -> None:
    with pytest.raises(CalendarError) as caught:
        read(FakeCalendarService(error=error))
    assert type(caught.value) is expected


def test_a_404_names_the_setting_to_check() -> None:
    with pytest.raises(CalendarError, match="calendar_id"):
        read(FakeCalendarService(error=FakeHttpError(404)))


def test_an_unexpected_error_is_a_bug_and_is_not_disguised() -> None:
    with pytest.raises(ZeroDivisionError):
        read(FakeCalendarService(error=ZeroDivisionError("bug")))


def test_error_messages_do_not_contain_the_response_body() -> None:
    error = FakeHttpError(403, "forbidden")
    error.content = b'{"error": {"message": "SECRET-DETAIL-4711"}}'
    with pytest.raises(CalendarAuthError) as caught:
        read(FakeCalendarService(error=error))
    assert "SECRET-DETAIL-4711" not in str(caught.value)


def test_the_real_service_can_be_built_without_any_network_access() -> None:
    credentials_module = importlib.import_module("google.oauth2.credentials")
    credentials: Any = credentials_module.Credentials(token="dummy-token-not-real")
    service = build_calendar_service(credentials)
    request: Any = service.events().list(calendarId="primary", singleEvents=True)
    base = "https://www.googleapis.com/calendar/v3/calendars/primary/events?"
    assert request.uri.startswith(base)  # built from the bundled discovery document, not downloaded


def test_the_window_end_is_exclusive_of_the_next_week() -> None:
    start, end = week_window(NOW)
    assert end - start == timedelta(days=7)
