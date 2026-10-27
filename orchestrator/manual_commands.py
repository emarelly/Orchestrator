"""Manual commands: On, Off, Auto and relay runs (FR-MAN-1..7, FR-LEG-6).

One class, :class:`ManualCommands`, is the single place every channel (web UI, MQTT, e-mail) calls,
so they all behave the same:

* the file is built from the **fresh** boiler status and the clock (:mod:`orchestrator.manual`) and
  validated (limit of 5, relays 2..7, duration), then uploaded **once**: manual commands are never
  retried automatically (CS-10);
* if the status is unknown, the boiler Pi is down or the reading is stale, manual On is **refused**
  instead of guessing a level;
* every attempt, accepted or refused, is written to the audit log with who asked and through which
  channel, and carries a correlation id that also appears in the log lines it produced
  (LOG-8, LOG-9);
* after a successful upload the status is read again so the answer can say what the boiler Pi now
  reports (FR-MAN-6); in dry-run mode the file is built and logged but nothing is sent.

Expected problems come back as a :class:`CommandResult` with ``ok=False``; they are not exceptions.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from orchestrator.boiler_client import BoilerError, NotConfigured, UploadResult
from orchestrator.clock import Clock
from orchestrator.config import LimitsConfig
from orchestrator.logging_setup import audit, correlation, kv
from orchestrator.manual import (
    ManualError,
    ManualUpload,
    build_auto,
    build_manual_off,
    build_manual_on,
    manual_upload,
    relay_off_upload,
    relay_run_upload,
)
from orchestrator.models import BoilerStatus, ModelError, format_number, format_time
from orchestrator.status_cache import ConnectionState, StatusCache
from orchestrator.validation import ValidationError
from orchestrator.weekdays import day_name

logger = logging.getLogger(__name__)


class ManualUploader(Protocol):
    """What the commands need from the boiler client."""

    def upload_manual(self, upload: ManualUpload) -> UploadResult: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of a manual command, ready to show to the user."""

    ok: bool
    sent: bool  # False for a refusal, a failure and for a dry run
    message: str
    correlation_id: str
    observed_mode: str | None = None  # what the boiler Pi reported after the upload
    filename: str | None = None


class ManualCommands:
    """Builds, validates and uploads the manual files on behalf of every channel."""

    def __init__(
        self,
        client: ManualUploader,
        cache: StatusCache,
        clock: Clock,
        limits: LimitsConfig,
        *,
        stale_after: timedelta = timedelta(minutes=5),
        refresh: Callable[[], object] | None = None,
    ) -> None:
        self._client = client
        self._cache = cache
        self._clock = clock
        self._limits = limits
        self._stale_after = stale_after
        self._refresh = refresh
        self._lock = threading.Lock()  # one command at a time, whatever the channel

    # --- the commands -------------------------------------------------------------------------

    def manual_on(self, *, source: str, who: str, hours: float | None = None) -> CommandResult:
        """Heat now: Target and Min above the current showers (never above the limit)."""

        def build(now: datetime) -> tuple[ManualUpload, str]:
            status = self._usable_status(now)
            override = build_manual_on(status, now, self._limits, hours)
            entry = override.entry
            assert entry is not None
            level = format_number(entry.target)
            until = f"{day_name(entry.expires_day)} {format_time(entry.expires_at)}"
            return manual_upload(override), f"Manual On: Target and Min {level}, until {until}"

        return self._execute("manual_on", source, who, build, hours=hours)

    def manual_off(self, *, source: str, who: str, hours: float | None = None) -> CommandResult:
        """Do not heat: Target 0 and Min 0 until the override expires."""

        def build(now: datetime) -> tuple[ManualUpload, str]:
            override = build_manual_off(now, self._limits, hours)
            entry = override.entry
            assert entry is not None
            until = f"{day_name(entry.expires_day)} {format_time(entry.expires_at)}"
            return manual_upload(override), f"Manual Off: Target and Min 0, until {until}"

        return self._execute("manual_off", source, who, build, hours=hours)

    def auto(self, *, source: str, who: str) -> CommandResult:
        """Back to the schedule: upload the empty manual file."""
        return self._execute(
            "manual_auto",
            source,
            who,
            lambda _now: (manual_upload(build_auto()), "Auto: the schedule applies again"),
        )

    def relay_on(self, relay: int, seconds: int, *, source: str, who: str) -> CommandResult:
        """Switch a relay (2..7) on for ``seconds`` seconds."""
        return self._execute(
            "relay_on",
            source,
            who,
            lambda now: (
                relay_run_upload(relay, seconds, now),
                f"Relay {relay} on for {seconds} s",
            ),
            relay=relay,
            seconds=seconds,
        )

    def relay_off(self, relay: int, *, source: str, who: str) -> CommandResult:
        """Switch a relay (2..7) off now (file form; the final payload is open, OQ-14)."""
        return self._execute(
            "relay_off",
            source,
            who,
            lambda now: (relay_off_upload(relay, now), f"Relay {relay} off"),
            relay=relay,
        )

    # --- internals ----------------------------------------------------------------------------

    def _usable_status(self, now: datetime) -> BoilerStatus:
        """Return the fresh status or explain precisely why manual On cannot go ahead."""
        snapshot = self._cache.snapshot()
        if snapshot.state is ConnectionState.DOWN:
            raise ManualError("the boiler Pi is not responding, so manual On cannot choose a level")
        status = self._cache.fresh_status(now, self._stale_after)
        if status is None:
            if snapshot.status is None:
                raise ManualError(
                    "the boiler status is unknown, so manual On cannot choose a level"
                )
            minutes = round(snapshot.status.age(now).total_seconds() / 60)
            raise ManualError(
                f"the last boiler reading is {minutes} min old, too old to choose a level"
            )
        return status

    def _execute(
        self,
        action: str,
        source: str,
        who: str,
        build: Callable[[datetime], tuple[ManualUpload, str]],
        **details: object,
    ) -> CommandResult:
        with self._lock, correlation() as cid:
            now = self._clock.now()
            try:
                upload, description = build(now)
            except (ManualError, ValidationError, ModelError) as error:
                return self._finish(cid, action, source, who, "refused", False, str(error), details)
            try:
                outcome = self._client.upload_manual(upload)  # exactly once, never retried
            except NotConfigured as error:
                return self._finish(
                    cid, action, source, who, "not_configured", False, str(error), details, upload
                )
            except BoilerError as error:
                return self._finish(
                    cid, action, source, who, "failed", False, f"not sent: {error}", details, upload
                )
            if not outcome.sent:
                text = f"{description} (dry run: the file was built and logged, not sent)"
                return self._finish(
                    cid, action, source, who, "dry_run", True, text, details, upload
                )
            observed = self._observe()
            text = description + (f"; the boiler Pi now reports {observed}" if observed else "")
            return self._finish(
                cid,
                action,
                source,
                who,
                "ok",
                True,
                text,
                details,
                upload,
                sent=True,
                observed=observed,
            )

    def _observe(self) -> str | None:
        """Read the status again after an upload and return the mode the boiler Pi reports."""
        if self._refresh is None:
            return None
        try:
            self._refresh()
        except Exception:  # the answer must still be delivered if the refresh misbehaves
            logger.exception(kv("manual_refresh_failed"))
            return None
        status = self._cache.snapshot().status
        return status.mode if status is not None else None

    def _finish(
        self,
        cid: str,
        action: str,
        source: str,
        who: str,
        result: str,
        ok: bool,
        message: str,
        details: dict[str, object],
        upload: ManualUpload | None = None,
        *,
        sent: bool = False,
        observed: str | None = None,
    ) -> CommandResult:
        audit(
            action,
            who=who,
            channel=source,
            result=result,
            file=upload.filename if upload else None,
            observed_mode=observed,
            detail=message if result != "ok" else None,
            **details,
        )
        log = logger.info if ok else logger.warning
        log(kv("manual_command", action=action, result=result, channel=source))
        return CommandResult(
            ok=ok,
            sent=sent,
            message=message,
            correlation_id=cid,
            observed_mode=observed,
            filename=upload.filename if upload else None,
        )
