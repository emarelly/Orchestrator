"""REST client for the boiler Pi (Requirements section 3.6; FR-PUSH-1..3, FR-STA-1, CS-9).

Every URL comes from :class:`~orchestrator.config.BoilerConfig`; every request has a timeout.
File uploads are **one** multipart POST (field ``file``) and nothing else: the legacy ``Post()``
sent a second, JSON request after a file upload and then reported failure. Manual control is also
one POST each: the boiler Pi builds its own manual files (``manualon/``, ``manualoff/``, ``auto/``,
``relayon/``), so nothing is uploaded for it (OQ-16).

Errors are specific so the callers can react correctly:

* :class:`BoilerUnreachable` - timeout or connection problem (retry later, show "unknown");
* :class:`BoilerRejected` - the Pi answered with a non-2xx status (do not mark a push as done);
* :class:`BoilerBadAnswer` - the answer is not what was expected (treat as unreadable);

In dry-run mode uploads are built and logged but never sent (CLAUDE.md safety rules); reads still
work. Response bodies are never logged in full.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

from orchestrator.config import BoilerConfig
from orchestrator.logging_setup import kv
from orchestrator.models import (
    RELAY_MAX,
    RELAY_MIN,
    BoilerStatus,
    HistoryParseError,
    HistorySample,
    StatusParseError,
    parse_history_text,
)

logger = logging.getLogger(__name__)

BOILER_SCHEDULE_FILENAME = "Cal.json"
TIMER_SCHEDULE_FILENAME = "TimeCal.json"
SNIPPET_LENGTH = 200


class BoilerError(Exception):
    """Base class of everything the boiler client raises."""


class BoilerUnreachable(BoilerError):
    """The boiler Pi did not answer (timeout, refused or dropped connection)."""


class BoilerRejected(BoilerError):
    """The boiler Pi answered with an HTTP status outside 200..299."""

    def __init__(self, status_code: int, snippet: str) -> None:
        super().__init__(f"HTTP {status_code}: {snippet}")
        self.status_code = status_code
        self.snippet = snippet


class BoilerBadAnswer(BoilerError):
    """The answer arrived but cannot be used (not JSON, wrong shape)."""


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Outcome of an upload or a command POST; ``sent`` is False in dry-run mode."""

    sent: bool
    status_code: int
    text: str


class BoilerClient:
    """Talks to the boiler Pi's REST API."""

    def __init__(
        self,
        config: BoilerConfig,
        *,
        session: requests.Session | None = None,
        dry_run: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._dry_run = dry_run
        self._monotonic = monotonic

    # --- reading ------------------------------------------------------------------------------

    def get_status(self) -> BoilerStatus:
        """Read ``status/``; raises if the Pi is down or the answer is unusable."""
        payload = self._get_json(self._config.status_path)
        if not isinstance(payload, dict):
            raise BoilerBadAnswer("status: expected a JSON object")
        try:
            return BoilerStatus.parse(payload)
        except StatusParseError as error:
            raise BoilerBadAnswer(f"status: {error}") from None

    def get_history(self) -> tuple[HistorySample, ...]:
        """Read ``history/`` (the last 24 hours for the chart)."""
        try:
            return parse_history_text(self._send("GET", self._config.history_path).text)
        except HistoryParseError as error:
            raise BoilerBadAnswer(str(error)) from None

    def get_log(self) -> str:
        """Read ``log/`` (the last lines of the boiler log) as text."""
        return self._send("GET", self._config.log_path).text

    def get_relay_status(self, relay: int) -> str:
        """Read the status / audit text of one relay."""
        if isinstance(relay, bool) or not RELAY_MIN <= relay <= RELAY_MAX:
            raise ValueError(f"relay must be {RELAY_MIN}..{RELAY_MAX}")
        return self._send("GET", self._config.relay_status_path + str(relay)).text

    # --- uploading ----------------------------------------------------------------------------

    def upload_boiler_schedule(self, content: bytes) -> UploadResult:
        """Replace ``Cal.json`` on the boiler Pi."""
        return self._upload(self._config.update_schedule_path, BOILER_SCHEDULE_FILENAME, content)

    def upload_timer_schedule(self, content: bytes) -> UploadResult:
        """Replace ``TimeCal.json`` on the boiler Pi."""
        return self._upload(self._config.update_timer_path, TIMER_SCHEDULE_FILENAME, content)

    # --- manual control: the boiler Pi builds its own manual files ------------------------------

    def manual_on(self) -> UploadResult:
        """Manual On: the boiler Pi heats to its own manual level for about 3 hours."""
        return self._command(self._config.manual_on_path)

    def manual_off(self) -> UploadResult:
        """Manual Off: no heating for about an hour."""
        return self._command(self._config.manual_off_path)

    def manual_auto(self) -> UploadResult:
        """Back to the schedule: the boiler Pi deletes its ``manual.json``."""
        return self._command(self._config.manual_auto_path)

    def relay_on(self, relay: int, seconds: int) -> UploadResult:
        """Switch a relay on for ``seconds`` (0 or less: on without a time limit).

        The boiler Pi acts about 90 s later and, with a duration, switches the relay off again.
        """
        return self._relay(self._config.relay_on_path, relay, seconds)

    def relay_off(self, relay: int, seconds: int = 0) -> UploadResult:
        """Switch a relay off for ``seconds``, then on again (0 or less: off, no time limit)."""
        return self._relay(self._config.relay_off_path, relay, seconds)

    def _relay(self, path: str, relay: int, seconds: int) -> UploadResult:
        if isinstance(relay, bool) or not RELAY_MIN <= relay <= RELAY_MAX:
            raise ValueError(f"relay must be {RELAY_MIN}..{RELAY_MAX}")
        if isinstance(seconds, bool):
            raise ValueError("seconds must be a whole number")
        # the key is spelt "swith" by the boiler service; 0 or less means no time limit
        return self._command(path, {"swith": relay, "duration": max(seconds, 0)})

    # --- internals ----------------------------------------------------------------------------

    def _get_json(self, path: str) -> object:
        response = self._send("GET", path)
        try:
            return response.json()
        except ValueError:
            raise BoilerBadAnswer(f"{path}: the answer is not valid JSON") from None

    def _upload(self, path: str, filename: str, content: bytes) -> UploadResult:
        if self._dry_run:
            logger.info(
                kv(
                    "boiler_upload_skipped",
                    reason="dry_run",
                    path=path,
                    file=filename,
                    size=len(content),
                )
            )
            return UploadResult(sent=False, status_code=0, text="dry run: not sent")
        response = self._send("POST", path, files={"file": (filename, content)})
        logger.info(
            kv(
                "boiler_upload",
                path=path,
                file=filename,
                size=len(content),
                status=response.status_code,
            )
        )
        return UploadResult(
            sent=True, status_code=response.status_code, text=_snippet(response.text)
        )

    def _command(self, path: str, payload: dict[str, int] | None = None) -> UploadResult:
        """POST a command (once, never retried); in dry-run mode only log it."""
        if self._dry_run:
            logger.info(kv("boiler_command_skipped", reason="dry_run", path=path))
            return UploadResult(sent=False, status_code=0, text="dry run: not sent")
        response = self._send("POST", path, payload=payload)
        logger.info(kv("boiler_command", path=path, status=response.status_code))
        return UploadResult(
            sent=True, status_code=response.status_code, text=_snippet(response.text)
        )

    def _send(
        self,
        method: str,
        path: str,
        *,
        files: dict[str, tuple[str, bytes]] | None = None,
        payload: dict[str, int] | None = None,
    ) -> requests.Response:
        url = self._config.url(path)
        started = self._monotonic()
        try:
            response = self._session.request(
                method,
                url,
                files=files,
                json=payload,
                timeout=self._config.request_timeout_s,
                allow_redirects=method == "GET",  # a redirected POST would silently become a GET
            )
        except requests.Timeout:
            raise BoilerUnreachable(f"{method} {path}: timed out") from None
        except requests.RequestException as error:
            raise BoilerUnreachable(f"{method} {path}: {type(error).__name__}") from None
        logger.debug(
            kv(
                "boiler_request",
                method=method,
                path=path,
                status=response.status_code,
                ms=round((self._monotonic() - started) * 1000),
            )
        )
        if not 200 <= response.status_code < 300:
            raise BoilerRejected(response.status_code, _snippet(response.text))
        return response


def _snippet(text: str) -> str:
    return text[:SNIPPET_LENGTH]
