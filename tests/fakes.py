"""Test doubles shared by the test modules."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import requests
from requests.adapters import BaseAdapter


class FakeClock:
    """A clock that only moves when the test says so (satisfies ``orchestrator.clock.Clock``)."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start time")
        self._now = start
        self._monotonic = 1000.0

    @property
    def tz(self) -> tzinfo:
        tz = self._now.tzinfo
        assert tz is not None
        return tz

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, delta: timedelta) -> None:
        """Move time forward by an absolute amount (daylight-saving changes are followed)."""
        self._now = (self._now.astimezone(UTC) + delta).astimezone(self.tz)
        self._monotonic += delta.total_seconds()


@dataclass(slots=True)
class RecordedRequest:
    """What the code under test sent."""

    method: str
    url: str
    body: bytes
    content_type: str
    timeout: Any


@dataclass(slots=True)
class _Route:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: Exception | None = None


class FakeBoilerAdapter(BaseAdapter):
    """Stands in for the network: records requests and answers from a table, never connects."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[RecordedRequest] = []
        self._routes: dict[tuple[str, str], _Route] = {}

    @staticmethod
    def normalize(url: str) -> str:
        """Return the URL as ``requests`` sends it (for example with a lower-case host name)."""
        return requests.Request("GET", url).prepare().url or url

    def route(
        self,
        method: str,
        url: str,
        *,
        status: int = 200,
        body: str | bytes = b"",
        headers: Mapping[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        """Define the answer (or the network error) for one method and URL."""
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self._routes[(method, self.normalize(url))] = _Route(
            status, raw, dict(headers or {}), error
        )

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        method, url = request.method or "", request.url or ""
        body = request.body if isinstance(request.body, bytes) else b""
        self.requests.append(
            RecordedRequest(
                method, url, body, str(request.headers.get("Content-Type", "")), timeout
            )
        )
        route = self._routes.get((method, url))
        if route is None:
            raise AssertionError(f"unexpected request to the fake boiler Pi: {method} {url}")
        if route.error is not None:
            raise route.error
        response = requests.Response()
        response.status_code = route.status
        response.url = url
        response.request = request
        response.headers.update(route.headers)
        response.raw = None
        response._content = route.body  # the public way to prefill a Response in a fake
        return response

    def close(self) -> None:
        return None


def fake_session(adapter: FakeBoilerAdapter) -> requests.Session:
    """Return a session whose http and https traffic goes to the fake adapter only."""
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
