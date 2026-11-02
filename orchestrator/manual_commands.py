"""Manual commands: On, Off, Auto and relay runs (FR-MAN-1..7, FR-LEG-6).

One class, :class:`ManualCommands`, is the single place every channel (web UI, MQTT, e-mail) calls,
so they all behave the same:

* each command is **one POST** to an endpoint of the boiler Pi (``manualon/``, ``manualoff/``,
  ``auto/``, ``relayon/``). The boiler Pi builds its own manual files and uses its own clock, so
  nothing is uploaded and the Orchestrator does not choose a level or an expiry time (OQ-16);
* the request is validated first (relays 2..7, duration) and sent **once**: manual commands are
  never retried automatically (CS-10);
* every attempt, accepted or refused, is written to the audit log with who asked and through which
  channel, and carries a correlation id that also appears in the log lines it produced
  (LOG-8, LOG-9);
* after a successful POST the status is read again so the answer can say what the boiler Pi now
  reports (FR-MAN-6); in dry-run mode the command is logged but nothing is sent.

Expected problems come back as a :class:`CommandResult` with ``ok=False``; they are not exceptions.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from orchestrator.boiler_client import BoilerError, UploadResult
from orchestrator.logging_setup import audit, correlation, kv
from orchestrator.status_cache import StatusCache
from orchestrator.validation import ValidationError, check_manual_relay

logger = logging.getLogger(__name__)

ON_TEXT = "Manual On: the boiler Pi heats to its own manual level for about 3 hours"
OFF_TEXT = "Manual Off: no heating for about an hour"
AUTO_TEXT = "Auto: the schedule applies again"
NO_DURATION_TEXT = (
    "the duration cannot be chosen: the boiler Pi runs manual On for about 3 hours and "
    "manual Off for about an hour"
)


class ManualError(ValueError):
    """A manual request cannot be sent (for example a duration the boiler Pi does not accept)."""


class BoilerCommands(Protocol):
    """What the commands need from the boiler client."""

    def manual_on(self) -> UploadResult: ...

    def manual_off(self) -> UploadResult: ...

    def manual_auto(self) -> UploadResult: ...

    def relay_on(self, relay: int, seconds: int) -> UploadResult: ...

    def relay_off(self, relay: int, seconds: int) -> UploadResult: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of a manual command, ready to show to the user."""

    ok: bool
    sent: bool  # False for a refusal, a failure and for a dry run
    message: str
    correlation_id: str
    observed_mode: str | None = None  # what the boiler Pi reported after the command


# a plan checks the request and returns what to send and how to describe it
_Plan = Callable[[], tuple[Callable[[], UploadResult], str]]


class ManualCommands:
    """Validates and sends the manual commands on behalf of every channel."""

    def __init__(
        self,
        client: BoilerCommands,
        cache: StatusCache,
        *,
        refresh: Callable[[], object] | None = None,
    ) -> None:
        self._client = client
        self._cache = cache
        self._refresh = refresh
        self._lock = threading.Lock()  # one command at a time, whatever the channel

    # --- the commands -------------------------------------------------------------------------

    def manual_on(self, *, source: str, who: str, hours: float | None = None) -> CommandResult:
        """Heat now: the boiler Pi applies its own manual level for about 3 hours."""

        def plan() -> tuple[Callable[[], UploadResult], str]:
            _no_duration(hours)
            return self._client.manual_on, ON_TEXT

        return self._execute("manual_on", source, who, plan, hours=hours)

    def manual_off(self, *, source: str, who: str, hours: float | None = None) -> CommandResult:
        """Do not heat: the boiler Pi stops heating for about an hour."""

        def plan() -> tuple[Callable[[], UploadResult], str]:
            _no_duration(hours)
            return self._client.manual_off, OFF_TEXT

        return self._execute("manual_off", source, who, plan, hours=hours)

    def auto(self, *, source: str, who: str) -> CommandResult:
        """Back to the schedule: the boiler Pi removes its manual override."""
        return self._execute(
            "manual_auto", source, who, lambda: (self._client.manual_auto, AUTO_TEXT)
        )

    def relay_on(self, relay: int, seconds: int, *, source: str, who: str) -> CommandResult:
        """Switch a relay (2..7) on for ``seconds``; 0 or less means on without a time limit."""

        def plan() -> tuple[Callable[[], UploadResult], str]:
            _check_relay(relay, seconds)
            if seconds > 0:
                text = f"Relay {relay} on for {seconds} s (the boiler Pi starts it in about 90 s)"
            else:
                text = (
                    f"Relay {relay} on without a time limit (the boiler Pi switches it in about "
                    "90 s; it stays on until it is switched off)"
                )
            return (lambda: self._client.relay_on(relay, seconds)), text

        return self._execute("relay_on", source, who, plan, relay=relay, seconds=seconds)

    def relay_off(self, relay: int, seconds: int = 0, *, source: str, who: str) -> CommandResult:
        """Switch a relay (2..7) off; with ``seconds`` it comes back on after that time."""

        def plan() -> tuple[Callable[[], UploadResult], str]:
            _check_relay(relay, seconds)
            if seconds > 0:
                text = (
                    f"Relay {relay} off for {seconds} s, then back on (the boiler Pi acts in "
                    "about 90 s)"
                )
            else:
                text = (
                    f"Relay {relay} off without a time limit (the boiler Pi switches it in "
                    "about 90 s)"
                )
            return (lambda: self._client.relay_off(relay, seconds)), text

        return self._execute("relay_off", source, who, plan, relay=relay, seconds=seconds)

    # --- internals ----------------------------------------------------------------------------

    def _execute(
        self, action: str, source: str, who: str, plan: _Plan, **details: object
    ) -> CommandResult:
        with self._lock, correlation() as cid:
            try:
                send, description = plan()
            except (ManualError, ValidationError) as error:
                return self._finish(cid, action, source, who, "refused", False, str(error), details)
            try:
                outcome = send()  # exactly once, never retried
            except BoilerError as error:
                return self._finish(
                    cid, action, source, who, "failed", False, f"not sent: {error}", details
                )
            if not outcome.sent:
                text = f"{description} (dry run: nothing was sent)"
                return self._finish(cid, action, source, who, "dry_run", True, text, details)
            observed = self._observe()
            text = description + (f"; the boiler Pi now reports {observed}" if observed else "")
            return self._finish(
                cid, action, source, who, "ok", True, text, details, sent=True, observed=observed
            )

    def _observe(self) -> str | None:
        """Read the status again after a command and return the mode the boiler Pi reports."""
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
        *,
        sent: bool = False,
        observed: str | None = None,
    ) -> CommandResult:
        audit(
            action,
            who=who,
            channel=source,
            result=result,
            observed_mode=observed,
            detail=message if result != "ok" else None,
            **details,
        )
        log = logger.info if ok else logger.warning
        log(kv("manual_command", action=action, result=result, channel=source))
        return CommandResult(
            ok=ok, sent=sent, message=message, correlation_id=cid, observed_mode=observed
        )


def _no_duration(hours: float | None) -> None:
    if hours is not None:
        raise ManualError(NO_DURATION_TEXT)


def _check_relay(relay: int, seconds: int) -> None:
    checked = check_manual_relay(relay, seconds)
    if not checked.ok:
        raise ValidationError(checked.errors)
