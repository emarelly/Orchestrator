"""Reading events from a calendar (FR-CFG-1, FR-GOOG-4, CS-9).

:class:`CalendarSource` is what the rest of the program depends on. :class:`GoogleCalendarSource`
implements it on top of the Google Calendar API v3 (``events.list`` with ``singleEvents=True``,
which expands recurring events into one event per day, like the legacy reader).

The Google service object is **passed in**, so the tests use a fake and nothing here touches the
network or credentials; :func:`build_calendar_service` creates the real one and imports the heavy
Google client only when it is first called (NFR-1a). Errors are sorted into the cases the caller
needs: :class:`CalendarAuthError` (authorization lost: show "Google authorization needed",
FR-GOOG-7) and :class:`CalendarUnavailable` (network or Google trouble: keep the last files and
retry later).
Error objects are recognised by their attributes, so this module needs no Google type stubs.
"""

import importlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Protocol

from google.auth.exceptions import RefreshError, TransportError

from orchestrator.logging_setup import kv

logger = logging.getLogger(__name__)

MAX_RESULTS_PER_PAGE = 250
MAX_PAGES = 10
RETRIES = 2
_RATE_LIMIT_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded"}


class CalendarError(Exception):
    """The calendar could not be read."""


class CalendarAuthError(CalendarError):
    """Google refused the credentials (expired, revoked or not allowed): authorization is needed."""


class CalendarUnavailable(CalendarError):
    """A network or Google-side problem; the last known schedules stay in force."""


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """One calendar event (one instance of a recurring event)."""

    event_id: str
    summary: str
    start: datetime  # aware, in the configured zone; midnight for an all-day event
    end: datetime | None
    all_day: bool = False
    updated: datetime | None = None
    recurring_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class CalendarRead:
    """The events of a window and a note for every event that was skipped."""

    events: tuple[CalendarEvent, ...]
    skipped: tuple[str, ...] = ()


class CalendarSource(Protocol):
    """Anything that can list the events of a time window."""

    def read(self, window_start: datetime, window_end: datetime) -> CalendarRead: ...


class _Request(Protocol):
    def execute(self, num_retries: int = 0) -> dict[str, Any]: ...


class _Events(Protocol):
    def list(self, **kwargs: Any) -> _Request: ...


class CalendarService(Protocol):
    """The part of the Google Calendar API resource that this module uses."""

    def events(self) -> _Events: ...


def schedule_window(now: datetime) -> tuple[datetime, datetime]:
    """Return the seven days that make the schedule: today 00:00 up to the same time a week later.

    The window rolls with the date instead of following the calendar week (Sunday to Saturday), so
    that on a Saturday the coming Sunday is already read, and a one-off event made for next week is
    taken as soon as it is within seven days. Every weekday occurs exactly once in the window, so
    the weekly schedule has no duplicate slots.
    """
    zone = now.tzinfo
    if zone is None:
        raise ValueError("a timezone-aware time is required")
    first = now.date()
    start = datetime.combine(first, time(0, 0), tzinfo=zone)
    end = datetime.combine(first + timedelta(days=7), time(0, 0), tzinfo=zone)
    return start, end


class GoogleCalendarSource:
    """Reads events from one Google calendar through an injected API service."""

    def __init__(self, service: CalendarService, calendar_id: str, tz: tzinfo) -> None:
        self._service = service
        self._calendar_id = calendar_id
        self._tz = tz

    def read(self, window_start: datetime, window_end: datetime) -> CalendarRead:
        """List the events in ``[window_start, window_end)``; raises :class:`CalendarError`."""
        events: list[CalendarEvent] = []
        skipped: list[str] = []
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            request = self._service.events().list(
                calendarId=self._calendar_id,
                timeMin=window_start.isoformat(),
                timeMax=window_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=MAX_RESULTS_PER_PAGE,
                pageToken=page_token,
            )
            try:
                response = request.execute(num_retries=RETRIES)
            except Exception as error:  # classified below; anything unexpected is re-raised
                classified = classify_error(error)
                if classified is None:
                    raise
                raise classified from None
            for item in response.get("items", []):
                event, note = self._convert(item)
                if event is not None:
                    events.append(event)
                elif note:
                    skipped.append(note)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        else:
            skipped.append(f"more than {MAX_PAGES} pages of events; the rest were not read")
        logger.debug(kv("calendar_read", events=len(events), skipped=len(skipped)))
        return CalendarRead(tuple(events), tuple(skipped))

    # --- converting one Google event ---------------------------------------------------------

    def _convert(self, item: dict[str, Any]) -> tuple[CalendarEvent | None, str | None]:
        event_id = str(item.get("id", "?"))
        if item.get("status") == "cancelled":
            return None, None
        summary = item.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return None, None  # an event without a title cannot be a command
        try:
            start, all_day = self._moment(item["start"])
            end_raw = item.get("end")
            end = self._moment(end_raw)[0] if end_raw else None
            updated_raw = item.get("updated")
            updated = (
                datetime.fromisoformat(updated_raw).astimezone(self._tz) if updated_raw else None
            )
        except (KeyError, TypeError, ValueError):
            return None, f"event {event_id}: unreadable start or end time"
        return (
            CalendarEvent(
                event_id,
                summary.strip(),
                start,
                end,
                all_day,
                updated,
                item.get("recurringEventId"),
            ),
            None,
        )

    def _moment(self, value: dict[str, Any]) -> tuple[datetime, bool]:
        if "dateTime" in value:
            parsed = datetime.fromisoformat(value["dateTime"])
            if parsed.tzinfo is None:  # Google always sends an offset; be safe if it does not
                parsed = parsed.replace(tzinfo=self._tz)
            return parsed.astimezone(self._tz), False
        day = date.fromisoformat(value["date"])
        return datetime.combine(day, time(0, 0), tzinfo=self._tz), True


# --- error classification -----------------------------------------------------------------------


def classify_error(error: Exception) -> CalendarError | None:
    """Map a Google or network error to a :class:`CalendarError`; ``None`` if it is a bug."""
    if isinstance(error, RefreshError):
        return CalendarAuthError("Google authorization was refused or has expired")
    if isinstance(error, TransportError | TimeoutError | OSError):
        return CalendarUnavailable(f"network problem: {type(error).__name__}")
    status = getattr(getattr(error, "resp", None), "status", None)
    if isinstance(status, int):  # a googleapiclient HttpError
        reason = _http_reason(error)
        if status == 401 or (status == 403 and reason not in _RATE_LIMIT_REASONS):
            return CalendarAuthError(f"Google refused access (HTTP {status})")
        if status == 404:
            return CalendarError(
                "the calendar was not found (HTTP 404); check [calendar] calendar_id"
            )
        return CalendarUnavailable(f"Google answered HTTP {status}")
    if type(error).__module__.startswith("httplib2"):  # connection errors of the HTTP layer
        return CalendarUnavailable(f"network problem: {type(error).__name__}")
    return None


def _http_reason(error: Exception) -> str:
    content = getattr(error, "content", b"")
    try:
        body = json.loads(content)
        return str(body["error"]["errors"][0]["reason"]).lower()
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def build_calendar_service(credentials: object, *, timeout_s: float = 20.0) -> CalendarService:
    """Create the real Google Calendar API service (imported only now, to spare memory).

    ``credentials`` is a ``google.oauth2`` credentials object from the authorization step. The
    bundled discovery document is used, so nothing is downloaded to build the service.
    """
    discovery = importlib.import_module("googleapiclient.discovery")
    httplib2 = importlib.import_module("httplib2")
    google_auth_httplib2 = importlib.import_module("google_auth_httplib2")
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout_s))
    service: CalendarService = discovery.build(
        "calendar", "v3", http=http, cache_discovery=False, static_discovery=True
    )
    return service
