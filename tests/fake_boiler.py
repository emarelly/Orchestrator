"""A fake boiler Pi for tests: a small Flask app plus an in-process "network" for ``requests``.

Nothing here opens a socket. :class:`FakeBoilerPi` answers the endpoints of Requirements section 3.6
the way the real Pi does as far as it is known, and lets a test break it on purpose (offline, slow,
HTTP errors, garbage answers, rejected uploads). Any request to another host fails the test.

Behaviour that is **assumed** and must be confirmed against the real boiler Pi:

* the two manual-file upload paths (``uploadmanual/`` and ``uploadrelaymanual/``) do not exist in
  ``Config.py`` yet (OQ-16): they are inventions of this fake, set through :func:`fake_config`;
* an uploaded empty ``manual.json`` (``[]``) removes the override, so ``Status`` returns to
  ``Auto`` (OQ-17); a non-empty one makes ``Status`` ``Manual`` with the uploaded Target / Min;
* uploads must be a multipart form with the field ``file`` and must be readable by the services'
  string-splitting parser (:mod:`tests.legacy_reader`), otherwise the fake answers 400.
"""

import copy
import dataclasses
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
FAKE_MANUAL_PATH = "uploadmanual/"
FAKE_RELAY_MANUAL_PATH = "uploadrelaymanual/"

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
    """The default boiler configuration plus the (invented) manual-file paths of the fake."""
    return dataclasses.replace(
        BoilerConfig(),
        manual_upload_path=FAKE_MANUAL_PATH,
        relay_manual_upload_path=FAKE_RELAY_MANUAL_PATH,
    )


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
        if self.config.manual_upload_path:
            self._route(self.config.manual_upload_path, "POST", self._upload_manual)
        if self.config.relay_manual_upload_path:
            self._route(self.config.relay_manual_upload_path, "POST", self._upload_plain)

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
        if content != b"[]" and not _readable_by_the_services(content):
            # "[]" is the empty manual file, handled by the manual endpoint
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

    def _upload_manual(self) -> Response:
        result = self._receive()
        if isinstance(result, Response):
            return result
        if result.content == b"[]":  # assumption (OQ-17): the empty file removes the override
            self.files.pop("manual.json", None)
            target, minimum = self._auto_values
            self.status.update(Status="Auto", TargetVal=target, MinVal=minimum)
        else:
            entry = json.loads(result.content)[0]
            self.files["manual.json"] = result.content
            self.status.update(
                Status="Manual", TargetVal=float(entry["Target"]), MinVal=float(entry["Min"])
            )
        return Response("ok", mimetype="text/plain")


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
