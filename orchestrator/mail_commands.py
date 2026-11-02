"""E-mail commands with two-factor confirmation (FR-LEG-3, FR-LEG-4, FR-MAN-6).

Only messages that :func:`orchestrator.mail_gate.classify_message` accepted reach this code.

``status`` and ``log`` are answered at once. Everything that changes something (``on``, ``off``,
``auto``, ``switch``, ``reset``) is first only *requested*: the reply has a random 10-letter token
as its subject, and the command runs only when a mail from the **same sender** brings the token back
(as the subject, a leading ``Re:`` is allowed) within the window, once. There is one pending command
for the whole system; a newer request replaces it. Wrong or late tokens count as failures; too many
lock e-mail commands for a while and the admin is told. The "done" mail reports what really
happened.
"""

import hmac
import logging
import re
import secrets
import string
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from orchestrator.clock import Clock
from orchestrator.logging_setup import audit, kv
from orchestrator.mail_client import MailClient, MailError, MailMessage, OutgoingMail
from orchestrator.mail_gate import (
    CommandName,
    ParsedCommand,
    SecurityAlerts,
    parse_command,
)
from orchestrator.manual_commands import CommandResult, ManualCommands
from orchestrator.models import BoilerStatus
from orchestrator.ssh_reboot import RebootError
from orchestrator.status_cache import StatusCache
from orchestrator.temperatures import levels_of

logger = logging.getLogger(__name__)

TOKEN_LENGTH = 10
LOG_LINES = 150
LOG_CHARS = 30_000
_TOKEN_LIKE = re.compile(rf"^[a-z]{{{TOKEN_LENGTH}}}$")
_REPLY_PREFIX = re.compile(r"^\s*(re|aw)\s*:\s*", re.IGNORECASE)
HELP = (
    "Commands (first word of the subject): status, log, on, off, auto, "
    "switch <relay 2-7> <seconds more than 10>, reset."
)


class Rebooter(Protocol):
    """Restarts the boiler Pi (the SSH implementation comes with the next item)."""

    def reboot(self) -> str: ...


class ResultKind(StrEnum):
    ANSWERED = "answered"  # status / log / a refused request / help
    REQUESTED = "requested"  # waiting for the token
    EXECUTED = "executed"  # confirmed and run (the outcome may still be a failure)
    FAILED_CONFIRMATION = "failed_confirmation"
    LOCKED_OUT = "locked_out"


@dataclass(frozen=True, slots=True)
class MailResult:
    kind: ResultKind
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _Pending:
    token: str
    command: ParsedCommand
    sender: str
    created: datetime  # UTC


def new_token() -> str:
    """Ten random lower-case letters from the operating system's secure source."""
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(TOKEN_LENGTH))


def describe(command: ParsedCommand) -> str:
    if command.name is CommandName.SWITCH:
        return f"switch relay {command.relay} on for {command.seconds} seconds"
    return {
        CommandName.ON: "manual ON (heat now, for about 3 hours)",
        CommandName.OFF: "manual OFF (no heating, for about an hour)",
        CommandName.AUTO: "AUTO (back to the schedule)",
        CommandName.RESET: "REBOOT of the boiler Pi",
    }.get(command.name, command.name.value)


def status_text(
    cache: StatusCache, now: datetime, stale_after: timedelta, *, dry_run: bool = False
) -> str:
    """The status as plain lines, honest about old or missing readings."""
    snapshot = cache.snapshot()
    status: BoilerStatus | None = snapshot.status
    lines = [f"Connection to the boiler Pi: {snapshot.state.value}"]
    if dry_run:
        lines.append("DRY RUN: nothing is sent to the boiler Pi")
    if status is None:
        return "\n".join([*lines, "No reading yet."])
    levels = levels_of(status)
    age = status.age(now)
    lines += [
        f"Mode: {status.mode}",
        f"Heater: {'on' if status.heater_on else 'off'}",
        f"Showers: {status.showers_thermo:.2f} (thermodynamic), "
        f"{status.showers_linear:.2f} (linear)",
        f"Target {status.target:.1f}, Min {status.minimum:.1f}",
        f"Temperature: upper {levels.upper:.1f}, mid {levels.mid:.1f}, lower {levels.lower:.1f}",
        f"Minutes to the next heating: {status.minutes_to_start}",
        f"Reading time: {status.date:%Y-%m-%d %H:%M:%S}" + (" (OLD)" if age > stale_after else ""),
    ]
    return "\n".join(lines)


class MailCommands:
    """The command and confirmation logic for accepted mail."""

    def __init__(
        self,
        client: MailClient,
        manual: ManualCommands,
        cache: StatusCache,
        clock: Clock,
        alerts: SecurityAlerts,
        *,
        log_text: Callable[[], str],
        rebooter: Rebooter | None = None,
        admin_email: str = "",
        window: timedelta = timedelta(seconds=120),
        max_failures: int = 5,
        lockout: timedelta = timedelta(minutes=15),
        stale_after: timedelta = timedelta(minutes=5),
        dry_run: bool = False,
        token_factory: Callable[[], str] = new_token,
    ) -> None:
        self._client = client
        self._manual = manual
        self._cache = cache
        self._clock = clock
        self._alerts = alerts
        self._log_text = log_text
        self._rebooter = rebooter
        self._admin = admin_email.strip()
        self._window = window
        self._max_failures = max_failures
        self._lockout = lockout
        self._stale_after = stale_after
        self._dry_run = dry_run
        self._token = token_factory
        self._lock = threading.Lock()
        self._pending: _Pending | None = None
        self._failures = 0
        self._locked_until: datetime | None = None

    # --- entry point ---------------------------------------------------------------------------

    def handle(self, message: MailMessage) -> MailResult:
        """Process one accepted message; replies to the sender and never raises on mail trouble."""
        with self._lock:
            now = self._clock.now().astimezone(UTC)
            if self._locked_until is not None:
                if now < self._locked_until:
                    return self._locked(message)
                self._locked_until = None
            parsed = parse_command(message.subject)
            if parsed is not None:
                if parsed.command is None:
                    return self._reply(message, message.subject[:60], f"{parsed.error}\n\n{HELP}")
                return self._command(message, parsed.command, now)
            token = _REPLY_PREFIX.sub("", message.subject).strip().lower()
            if _TOKEN_LIKE.match(token):
                return self._confirm(message, token, now)
            return self._reply(message, "Unknown command", f"I did not understand that.\n\n{HELP}")

    # --- a new command -------------------------------------------------------------------------

    def _command(self, message: MailMessage, command: ParsedCommand, now: datetime) -> MailResult:
        who = f"mail:{message.sender}"
        if not command.changes_state:
            audit("mail_command", who=who, command=command.name.value, stage="answered")
            text = self._status() if command.name is CommandName.STATUS else self._log()
            return self._reply(message, command.name.value, text)
        token = self._token()
        self._pending = _Pending(token, command, message.sender, now)
        audit("mail_command", who=who, command=command.name.value, stage="requested")
        body = (
            f"Pending: {describe(command)}\n\n{self._status()}\n\n"
            f"Nothing has changed yet. To run it, reply to this mail within "
            f"{int(self._window.total_seconds())} seconds with the subject exactly as it is "
            f"(a leading 'Re:' is fine). The token works once. A new command replaces this one."
        )
        self._send(message.sender, token, body)
        return MailResult(ResultKind.REQUESTED, command.name.value)

    # --- a token comes back --------------------------------------------------------------------

    def _confirm(self, message: MailMessage, token: str, now: datetime) -> MailResult:
        pending = self._pending
        who = f"mail:{message.sender}"
        if pending is None:
            return self._failed(message, now, "there is no pending command (it may have expired)")
        if message.sender != pending.sender:
            return self._failed(message, now, "the token was sent by a different sender")
        if now - pending.created > self._window:
            self._pending = None
            audit("mail_command", who=who, command=pending.command.name.value, stage="expired")
            return self._failed(message, now, "the token has expired; send the command again")
        if not hmac.compare_digest(token.encode(), pending.token.encode()):
            return self._failed(message, now, "the token is wrong")
        self._pending = None  # one use only
        self._failures = 0
        audit("mail_command", who=who, command=pending.command.name.value, stage="confirmed")
        outcome = self._execute(pending.command, who)
        audit("mail_command", who=who, command=pending.command.name.value, stage="done")
        self._send(message.sender, f"{pending.command.name.value}: {outcome}", outcome)
        return MailResult(ResultKind.EXECUTED, outcome)

    def _failed(self, message: MailMessage, now: datetime, why: str) -> MailResult:
        self._failures += 1
        logger.warning(kv("mail_confirmation_failed", sender=message.sender, why=why))
        audit("mail_command", who=f"mail:{message.sender}", stage="confirmation_failed", why=why)
        self._alerts.event(message.sender, f"confirmation token rejected: {why}", message)
        if self._failures >= self._max_failures:
            self._lock_out(now)
            self._send(
                message.sender,
                "E-mail commands blocked",
                "Too many wrong or late tokens. E-mail commands are blocked for "
                f"{int(self._lockout.total_seconds() // 60)} minutes.",
            )
            return MailResult(ResultKind.LOCKED_OUT, why)
        self._send(message.sender, "Command not confirmed", f"Not done: {why}.\n\n{HELP}")
        return MailResult(ResultKind.FAILED_CONFIRMATION, why)

    def _lock_out(self, now: datetime) -> None:
        self._locked_until = now + self._lockout
        self._failures = 0
        self._pending = None
        logger.error(kv("mail_commands_locked", minutes=int(self._lockout.total_seconds() // 60)))
        audit("mail_command", who="mail", stage="locked_out")
        if self._admin:
            self._send(
                self._admin,
                "E-mail commands locked",
                f"E-mail commands were blocked for {int(self._lockout.total_seconds() // 60)} "
                f"minutes after {self._max_failures} wrong or late confirmation tokens.",
            )

    def _locked(self, message: MailMessage) -> MailResult:
        self._send(
            message.sender, "E-mail commands blocked", "E-mail commands are blocked for now."
        )
        return MailResult(ResultKind.LOCKED_OUT, "locked")

    # --- running a confirmed command -----------------------------------------------------------

    def _execute(self, command: ParsedCommand, who: str) -> str:
        result: CommandResult | None = None
        if command.name is CommandName.ON:
            result = self._manual.manual_on(source="email", who=who)
        elif command.name is CommandName.OFF:
            result = self._manual.manual_off(source="email", who=who)
        elif command.name is CommandName.AUTO:
            result = self._manual.auto(source="email", who=who)
        elif command.name is CommandName.SWITCH and command.relay and command.seconds:
            result = self._manual.relay_on(command.relay, command.seconds, source="email", who=who)
        elif command.name is CommandName.RESET:
            return self._reboot()
        if result is None:
            return "FAILED: the command is not supported."
        status = "DONE" if result.ok else "FAILED"
        sent = "" if result.sent or not result.ok else " (dry run: nothing was sent)"
        seen = (
            f"\nThe boiler Pi now reports: {result.observed_mode}" if result.observed_mode else ""
        )
        return f"{status}: {result.message}{sent}{seen}"

    def _reboot(self) -> str:
        if self._rebooter is None:
            return "FAILED: rebooting the boiler Pi is not configured."
        if self._dry_run:
            return "DONE (dry run): the boiler Pi was not rebooted."
        try:
            return "DONE: " + self._rebooter.reboot()
        except RebootError as error:  # its text was written to be shown
            logger.error(kv("reboot_failed", error=str(error)))
            return f"FAILED: {error}."
        except Exception as error:  # anything else: only the kind, never the text
            logger.error(kv("reboot_failed", error=type(error).__name__))
            return f"FAILED: the reboot did not work ({type(error).__name__})."

    # --- texts and mail ------------------------------------------------------------------------

    def _status(self) -> str:
        return status_text(self._cache, self._clock.now(), self._stale_after, dry_run=self._dry_run)

    def _log(self) -> str:
        try:
            text = self._log_text()
        except Exception as error:  # the boiler Pi may be down; say so instead of failing
            return f"The log could not be read ({type(error).__name__})."
        lines = text.splitlines()[-LOG_LINES:]
        return "\n".join(lines)[-LOG_CHARS:] or "The log is empty."

    def _reply(self, message: MailMessage, subject: str, body: str) -> MailResult:
        self._send(message.sender, f"Re: {subject}" if subject else "Re:", body)
        return MailResult(ResultKind.ANSWERED, subject)

    def _send(self, to: str, subject: str, body: str) -> None:
        try:
            self._client.send(OutgoingMail(to, subject, body))
        except MailError as error:
            logger.error(kv("mail_reply_failed", error=str(error)))
