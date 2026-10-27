"""Creating, changing and deleting calendar events (FR-CFG-4).

A thin wrapper around the Google Calendar API v3 (``events.insert`` / ``get`` / ``patch`` /
``delete``). Like the reader it takes the API service as an argument (so tests use a fake and the
heavy Google client is not imported here) and reuses the same error classification.

Events created here carry a private marker (``extendedProperties.private.managedBy``), so the editor
can tell the events this program owns from those the user made by hand.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Protocol

from orchestrator.calendar_source import CalendarError, classify_error

MANAGED_KEY = "managedBy"
MANAGED_VALUE = "orchestrator"
DEFAULT_LENGTH = timedelta(minutes=15)
RETRIES = 2


@dataclass(frozen=True, slots=True)
class RemoteEvent:
    """An event as Google holds it right now (a recurring series is one event, the "master")."""

    event_id: str
    summary: str
    start: datetime | None  # None for an event that cannot be read
    updated: datetime | None
    recurrence: tuple[str, ...]
    managed: bool
    all_day: bool = False


class _Request(Protocol):
    def execute(self, num_retries: int = 0) -> Any: ...


class _WriterEvents(Protocol):
    def insert(self, *, calendarId: str, body: dict[str, Any]) -> _Request: ...

    def get(self, *, calendarId: str, eventId: str) -> _Request: ...

    def patch(self, *, calendarId: str, eventId: str, body: dict[str, Any]) -> _Request: ...

    def delete(self, *, calendarId: str, eventId: str) -> _Request: ...


class WriterService(Protocol):
    """The part of the Google Calendar API resource that the writer uses."""

    def events(self) -> _WriterEvents: ...


class GoogleCalendarWriter:
    """Writes events to one Google calendar through an injected API service."""

    def __init__(self, service: WriterService, calendar_id: str, tz: tzinfo) -> None:
        self._service = service
        self._calendar_id = calendar_id
        self._tz = tz

    def get(self, event_id: str) -> RemoteEvent:
        """Read one event (the master of a series if given its id)."""
        item = self._call(
            lambda: self._service.events().get(calendarId=self._calendar_id, eventId=event_id)
        )
        return self._remote(item)

    def create(
        self,
        *,
        summary: str,
        start: datetime,
        recurrence: Sequence[str] = (),
        length: timedelta = DEFAULT_LENGTH,
    ) -> RemoteEvent:
        """Create an event owned by this program, optionally a weekly series."""
        body: dict[str, Any] = {
            "summary": summary,
            "start": self._moment(start, bool(recurrence)),
            "end": self._moment(start + length, bool(recurrence)),
            "extendedProperties": {"private": {MANAGED_KEY: MANAGED_VALUE}},
        }
        if recurrence:
            body["recurrence"] = list(recurrence)
        item = self._call(
            lambda: self._service.events().insert(calendarId=self._calendar_id, body=body)
        )
        return self._remote(item)

    def patch(
        self,
        event_id: str,
        *,
        summary: str | None = None,
        start: datetime | None = None,
        recurrence: Sequence[str] | None = None,
        length: timedelta = DEFAULT_LENGTH,
    ) -> RemoteEvent:
        """Change the given fields of an event (and of a whole series if given its master)."""
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        if start is not None:
            repeating = bool(recurrence)
            body["start"] = self._moment(start, repeating)
            body["end"] = self._moment(start + length, repeating)
        if recurrence is not None:
            body["recurrence"] = list(recurrence)
        item = self._call(
            lambda: self._service.events().patch(
                calendarId=self._calendar_id, eventId=event_id, body=body
            )
        )
        return self._remote(item)

    def delete(self, event_id: str) -> None:
        """Delete an event or a whole series; an event that is already gone counts as deleted."""
        try:
            self._call(
                lambda: self._service.events().delete(
                    calendarId=self._calendar_id, eventId=event_id
                )
            )
        except CalendarError as error:
            if "HTTP 404" in str(error) or "HTTP 410" in str(error):  # not found / already gone
                return
            raise

    # --- internals ----------------------------------------------------------------------------

    def _moment(self, value: datetime, repeating: bool) -> dict[str, str]:
        result = {"dateTime": value.astimezone(self._tz).isoformat()}
        zone = getattr(self._tz, "key", None)
        if isinstance(zone, str):
            result["timeZone"] = zone  # Google needs a named zone for a recurring event
        elif repeating:
            raise CalendarError(
                "a named time zone (for example Asia/Jerusalem) is needed to repeat"
            )
        return result

    def _call(self, make_request: Any) -> Any:
        try:
            return make_request().execute(num_retries=RETRIES)
        except Exception as error:  # classified below; anything unexpected is re-raised
            classified = classify_error(error)
            if classified is None:
                raise
            raise classified from None

    def _remote(self, item: dict[str, Any]) -> RemoteEvent:
        start_raw = item.get("start") or {}
        start: datetime | None
        all_day = "dateTime" not in start_raw and "date" in start_raw
        try:
            if "dateTime" in start_raw:
                start = datetime.fromisoformat(start_raw["dateTime"]).astimezone(self._tz)
            elif all_day:
                start = datetime.combine(
                    date.fromisoformat(start_raw["date"]), time(0, 0), self._tz
                )
            else:
                start = None
            updated_raw = item.get("updated")
            updated = (
                datetime.fromisoformat(updated_raw).astimezone(self._tz) if updated_raw else None
            )
        except ValueError:
            start, updated = None, None
        private = (item.get("extendedProperties") or {}).get("private") or {}
        return RemoteEvent(
            event_id=str(item.get("id", "")),
            summary=str(item.get("summary", "")),
            start=start,
            updated=updated,
            recurrence=tuple(str(rule) for rule in item.get("recurrence") or ()),
            managed=private.get(MANAGED_KEY) == MANAGED_VALUE,
            all_day=all_day,
        )
