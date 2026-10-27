"""A fake Google Calendar API service for tests: no network, no credentials.

``FakeCalendarService`` has the shape that ``GoogleCalendarSource`` uses
(``service.events().list(...).execute(num_retries=...)``) and returns the pages it was given.
``FakeHttpError`` carries the attributes of a real ``googleapiclient.errors.HttpError``
(``resp.status`` and the JSON ``content``) without importing the Google client.
"""

import copy
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any


class FakeHttpError(Exception):
    """Looks like ``googleapiclient.errors.HttpError`` to the code under test."""

    def __init__(self, status: int, reason: str = "") -> None:
        super().__init__(f"HttpError {status} {reason}")
        self.resp = SimpleNamespace(status=status)
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode("utf-8")


class _FakeRequest:
    def __init__(self, service: "FakeCalendarService", index: int) -> None:
        self._service = service
        self._index = index

    def execute(self, num_retries: int = 0) -> dict[str, Any]:
        self._service.retries.append(num_retries)
        if self._service.error is not None:
            raise self._service.error
        pages = self._service.pages
        index = min(self._index, len(pages) - 1) if self._service.stable else self._index
        return pages[index] if index < len(pages) else {"items": []}


class FakeCalendarService:
    """Answers ``events().list(...)`` with the given pages, one per call."""

    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        *,
        stable: bool = False,
    ) -> None:
        self.pages = pages or [{"items": []}]
        self.error = error
        self.stable = stable  # True: after the last page, keep answering with it (repeated reads)
        self.calls: list[dict[str, Any]] = []
        self.retries: list[int] = []

    def events(self) -> "FakeCalendarService":
        return self

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.calls.append(kwargs)
        return _FakeRequest(self, len(self.calls) - 1)


_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")  # index = Python weekday()


class _StoreRequest:
    def __init__(
        self, store: "FakeCalendarStore", verb: str, event_id: str | None, body: Any
    ) -> None:
        self._store = store
        self._args = (verb, event_id, body)

    def execute(self, num_retries: int = 0) -> Any:
        return self._store.run(*self._args)


class FakeCalendarStore:
    """An in-memory calendar with insert / get / patch / delete / list (weekly series expanded).

    ``now`` supplies the ``updated`` stamp of every change, so a test controls "who changed last".
    """

    def __init__(self, now: Callable[[], datetime]) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.verbs: list[tuple[str, str | None]] = []  # every request, in order
        self.error: Exception | None = None
        self._now = now
        self._counter = 0

    # the part of the API resource that the code under test uses
    def events(self) -> "FakeCalendarStore":
        return self

    def insert(self, *, calendarId: str, body: dict[str, Any]) -> _StoreRequest:
        return _StoreRequest(self, "insert", None, body)

    def get(self, *, calendarId: str, eventId: str) -> _StoreRequest:
        return _StoreRequest(self, "get", eventId, None)

    def patch(self, *, calendarId: str, eventId: str, body: dict[str, Any]) -> _StoreRequest:
        return _StoreRequest(self, "patch", eventId, body)

    def delete(self, *, calendarId: str, eventId: str) -> _StoreRequest:
        return _StoreRequest(self, "delete", eventId, None)

    # test helpers
    def seed(
        self,
        event_id: str,
        summary: str,
        start: str,
        *,
        recurrence: list[str] | None = None,
        managed: bool = False,
        updated: datetime | None = None,
        all_day: bool = False,
    ) -> None:
        item = google_event(summary, start, id=event_id, all_day=all_day)
        item["updated"] = (updated or self._now()).isoformat()
        if recurrence:
            item["recurrence"] = recurrence
        if managed:
            item["extendedProperties"] = {"private": {"managedBy": "orchestrator"}}
        self.items[event_id] = item

    def touch(self, event_id: str, when: datetime) -> None:
        """Simulate a change made in Google Calendar itself at the given time."""
        self.items[event_id]["updated"] = when.isoformat()

    def writes(self) -> list[tuple[str, str | None]]:
        return [v for v in self.verbs if v[0] in ("insert", "patch", "delete")]

    # the fake server
    def run(self, verb: str, event_id: str | None, body: Any) -> Any:
        self.verbs.append((verb, event_id))
        if self.error is not None:
            raise self.error
        if verb == "insert":
            self._counter += 1
            item = copy.deepcopy(body)
            item.update(
                id=f"new-{self._counter}", status="confirmed", updated=self._now().isoformat()
            )
            self.items[item["id"]] = item
            return copy.deepcopy(item)
        if verb == "list":
            return {"items": self._instances(body["timeMin"], body["timeMax"])}
        assert event_id is not None
        if event_id not in self.items:
            raise FakeHttpError(404)
        if verb == "get":
            return copy.deepcopy(self.items[event_id])
        if verb == "patch":
            self.items[event_id].update(copy.deepcopy(body))
            self.items[event_id]["updated"] = self._now().isoformat()
            return copy.deepcopy(self.items[event_id])
        del self.items[event_id]  # delete
        return ""

    def _instances(self, time_min: str, time_max: str) -> list[dict[str, Any]]:
        low, high = datetime.fromisoformat(time_min), datetime.fromisoformat(time_max)
        found: list[dict[str, Any]] = []
        for item in self.items.values():
            start = datetime.fromisoformat(item["start"]["dateTime"])
            rules = [r for r in item.get("recurrence", []) if r.upper().startswith("RRULE:")]
            if not rules:
                if low <= start < high:
                    found.append(copy.deepcopy(item))
                continue
            fields = dict(p.split("=", 1) for p in rules[0][6:].split(";") if "=" in p)
            days = fields.get("BYDAY", _WEEKDAY_CODES[start.weekday()]).split(",")
            day = low.date()
            while day < high.date():
                moment = datetime.combine(day, start.timetz().replace(tzinfo=None), low.tzinfo)
                if (
                    _WEEKDAY_CODES[day.weekday()] in days
                    and day >= start.date()
                    and low <= moment < high
                ):
                    instance = copy.deepcopy(item)
                    instance.pop("recurrence", None)
                    instance["id"] = f"{item['id']}_{day.isoformat()}"
                    instance["recurringEventId"] = item["id"]
                    instance["start"] = {"dateTime": moment.isoformat()}
                    found.append(instance)
                day += timedelta(days=1)
        return found

    # defined last: a method called `list` shadows the builtin in the class body below it
    def list(self, **kwargs: Any) -> _StoreRequest:
        return _StoreRequest(self, "list", None, kwargs)


def google_event(
    summary: str | None,
    start: str,
    end: str | None = None,
    *,
    all_day: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """Build an event dict the way the Google Calendar API returns it."""
    key = "date" if all_day else "dateTime"
    event: dict[str, Any] = {
        "id": extra.pop("id", "evt-" + start),
        "status": extra.pop("status", "confirmed"),
        "start": {key: start},
        "end": {key: end or start},
    }
    if summary is not None:
        event["summary"] = summary
    event.update(extra)
    return event
