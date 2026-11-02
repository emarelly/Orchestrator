"""A fake boiler Pi for tests: a small Flask app plus an in-process "network" for ``requests``.

Nothing here opens a socket. :class:`FakeBoilerPi` answers the endpoints of Requirements section 3.6
the way the real Pi does as far as it is known, and lets a test break it on purpose (offline, slow,
HTTP errors, garbage answers, rejected uploads). Any request to another host fails the test.

The manual endpoints follow ``ref files/ManagerRestApi.py`` (OQ-16): ``manualon/`` makes ``Status``
``Manual`` with Target and Min 4, ``manualoff/`` makes it ``Manual`` with 0 and 0, ``auto/`` returns
it to ``Auto``. ``relayon/`` and ``relayoff/`` take the JSON line
``{"swith": relay, "duration": seconds}`` (a duration of 0 means no time limit); the fake only
records them.

Behaviour that is **assumed** and must be confirmed against the real boiler Pi:

* the schedule uploads must be a multipart form with the field ``file`` and must be readable by
  the services' string-splitting parser (:mod:`tests.legacy_reader`), otherwise the fake answers
  400;
* ``reject_uploads_with`` makes every POST answer with that status, commands included.
"""

import copy
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, request
from requests.adapters import BaseAdapter

from orchestrator.config import BoilerConfig
from tests import legacy_reader

HOST = "marellypi3:8000"  # requests lower-cases the host name

DEFAULT_STATUS: dict[str, Any] = {
    "ActiveYesNo": "Yes",
    "CurrentShowers": 3.43,
    "CurrentShowersnew": 4.92,
    "CurrentTemp": [58.06, 54.19, 61.44],
    "Date": "2026-10-24T16:40:19.707843",
    "HeaterOnOff": "Off",
    "MinVal": 2.0,
    "Status": "Auto",
    "TargetVal": 3.0,
    "TimeToStartMin": 175,
}


def fake_config() -> BoilerConfig:
    """The default boiler configuration: the fake answers on the real paths."""
    return BoilerConfig()


@dataclass(frozen=True, slots=True)
class Upload:
    """One file the fake received."""

    path: str
    filename: str
    content: bytes


class FakeBoilerPi(BaseAdapter):
    """The fake boiler Pi: state, failure switches and the ``requests`` adapter in one object."""

    def __init__(self, config: BoilerConfig | None = None) -> None:
        super().__init__()
        self.config = config or fake_config()
        self.status: dict[str, Any] = copy.deepcopy(DEFAULT_STATUS)
        self.history_rows: list[list[float]] = []
        self.log_lines: list[str] = ["boiler service started"]
        # what the fake received
        self.files: dict[str, bytes] = {}
        self.uploads: list[Upload] = []
        self.commands: list[tuple[str, dict[str, Any] | None]] = []  # manual endpoints called
        self.calls: list[tuple[str, str]] = []  # (method, path)
        # failure switches
        self.offline = False
        self.delay_s = 0.0
        self.garbage_status = False
        self.reject_uploads_with: int | None = None
        self._fail_next: list[int] = []
        self._auto_values = (float(self.status["TargetVal"]), float(self.status["MinVal"]))

        self.app = Flask("fake-boiler")
        self._http = self.app.test_client()
        self._http_lock = threading.Lock()  # the Flask test client is not for several threads
        self._route(self.config.status_path, "GET", self._status)
        self._route(self.config.history_path, "GET", self._history)
        self._route(self.config.log_path, "GET", self._log)
        self._route(self.config.relay_status_path.split("?")[0], "GET", self._relay)
        self._route(self.config.update_schedule_path, "POST", self._upload_plain)
        self._route(self.config.update_timer_path, "POST", self._upload_plain)
        self._route(self.config.manual_on_path, "POST", self._manual_on)
        self._route(self.config.manual_off_path, "POST", self._manual_off)
        self._route(self.config.manual_auto_path, "POST", self._manual_auto)
        self._route(self.config.relay_on_path, "POST", self._relay_on)
        self._route(self.config.relay_off_path, "POST", self._relay_off)

    # --- test controls ------------------------------------------------------------------------

    def session(self) -> requests.Session:
        """Return a ``requests`` session whose traffic goes only to this fake."""
        session = requests.Session()
        session.mount("http://", self)
        session.mount("https://", self)
        return session

    def fail_next(self, count: int, status: int = 500) -> None:
        """Answer the next ``count`` requests with an HTTP error status."""
        self._fail_next = [status] * count

    # --- the in-process network --------------------------------------------------------------

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        parts = urlsplit(request.url or "")
        if parts.netloc != HOST:
            raise AssertionError(f"request to an unexpected host: {parts.netloc!r}")
        method = request.method or "GET"
        self.calls.append((method, parts.path))
        if self.offline:
            raise requests.ConnectionError("fake: the boiler Pi is offline")
        if self.delay_s > _timeout_seconds(timeout):
            raise requests.ReadTimeout("fake: the boiler Pi answered too slowly")
        if self._fail_next:
            return _response(request, self._fail_next.pop(0), b"fake: injected failure", {})
        body = request.body if isinstance(request.body, bytes) else None
        with self._http_lock:
            answer = self._http.open(
                parts.path,
                method=method,
                query_string=parts.query,
                data=body,
                headers={"Content-Type": str(request.headers.get("Content-Type", ""))},
            )
        return _response(request, answer.status_code, answer.get_data(), dict(answer.headers))

    def close(self) -> None:
        return None

    # --- the endpoints ------------------------------------------------------------------------

    def _route(self, path: str, method: str, view: Callable[[], Response]) -> None:
        rule = "/" + path.strip("/") + "/"
        self.app.add_url_rule(rule, endpoint=f"{method}{rule}", view_func=view, methods=[method])

    def _status(self) -> Response:
        if self.garbage_status:
            return Response("<html>Service Unavailable</html>", mimetype="text/html")
        return Response(json.dumps(self.status), mimetype="application/json")

    def _history(self) -> Response:
        return Response(json.dumps(self.history_rows), mimetype="application/json")

    def _log(self) -> Response:
        return Response("\n".join(self.log_lines), mimetype="text/plain")

    def _relay(self) -> Response:
        return Response(f"relay {request.args.get('relay', '?')}: off", mimetype="text/plain")

    def _receive(self) -> Upload | Response:
        """Check and record an upload; returns the upload, or the error answer to send."""
        if self.reject_uploads_with is not None:
            return Response("fake: uploads are rejected", status=self.reject_uploads_with)
        uploaded = request.files.get("file")
        if uploaded is None:
            return Response("fake: the multipart field 'file' is missing", status=400)
        content = uploaded.read()
        if not _readable_by_the_services(content):
            return Response("fake: the services could not read this file", status=400)
        upload = Upload(request.path, uploaded.filename or "", content)
        self.uploads.append(upload)
        return upload

    def _upload_plain(self) -> Response:
        result = self._receive()
        if isinstance(result, Response):
            return result
        self.files[result.filename] = result.content
        return Response("ok", mimetype="text/plain")

    def _command(self, name: str, body: dict[str, Any] | None = None) -> Response | None:
        """Record a command; returns the error answer when uploads are set to be rejected."""
        self.commands.append((name, body))
        if self.reject_uploads_with is not None:
            return Response("fake: commands are rejected", status=self.reject_uploads_with)
        return None

    def _manual_on(self) -> Response:
        return self._command("manual_on") or self._set_manual(4.0, 4.0)

    def _manual_off(self) -> Response:
        return self._command("manual_off") or self._set_manual(0.0, 0.0)

    def _manual_auto(self) -> Response:
        refused = self._command("auto")
        if refused is not None:
            return refused
        target, minimum = self._auto_values
        self.status.update(Status="Auto", TargetVal=target, MinVal=minimum)
        return Response("ok", mimetype="text/plain")

    def _set_manual(self, target: float, minimum: float) -> Response:
        self.status.update(Status="Manual", TargetVal=target, MinVal=minimum)
        return Response("ok", mimetype="text/plain")

    def _relay_on(self) -> Response:
        return self._relay_command("relay_on")

    def _relay_off(self) -> Response:
        return self._relay_command("relay_off")

    def _relay_command(self, name: str) -> Response:
        # the real handlers read the first line of the body as JSON
        body = request.get_data(as_text=True).splitlines()
        try:
            data = json.loads(body[0])
            relay, duration = int(data["swith"]), int(data["duration"])
        except (IndexError, ValueError, KeyError, TypeError):
            return Response("fake: expected one JSON line with swith and duration", status=400)
        return self._command(name, {"swith": relay, "duration": duration}) or Response(
            "ok", mimetype="text/plain"
        )


def _readable_by_the_services(content: bytes) -> bool:
    """True if the services' string-splitting reader gets sane entries out of the file.

    A misread file does not always crash that reader; it can silently produce entries with the
    default day 0 or no ``HH:MM:SS`` hour, which the fake treats as unreadable.
    """
    try:
        tasks = legacy_reader.parse(content.decode("utf-8"))
    except (ValueError, IndexError, UnicodeDecodeError):
        return False
    return bool(tasks) and all(
        isinstance(task["day"], int)
        and 1 <= task["day"] <= 7
        and re.fullmatch(r"\d\d:\d\d:\d\d", str(task["hour"])) is not None
        for task in tasks
    )


def _timeout_seconds(timeout: Any) -> float:
    if isinstance(timeout, tuple):
        return float(max(timeout))
    return float(timeout) if timeout is not None else float("inf")


def _response(
    request: requests.PreparedRequest, status: int, body: bytes, headers: dict[str, str]
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = request.url or ""
    response.request = request
    response.headers.update(headers)
    response.raw = None
    response._content = body
    return response
