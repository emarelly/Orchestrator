"""Who may command the boiler by e-mail, and what is said about those who may not (FR-LEG-3b).

Three small pieces, none of which changes anything on the boiler:

* :func:`classify_message` decides, from facts about a message, whether it is accepted, quietly
  ignored (our own mail, automatic mail) or rejected (not on the list, or not authenticated);
* :func:`parse_command` recognises the command by the **first word** of the subject and checks its
  arguments with the same rules as every other channel;
* :class:`SecurityAlerts` e-mails the alert address about a rejection, rate limited so that an
  attacker (or a loop) cannot fill the inbox.

A rejected sender is never answered, so the address is not confirmed to a stranger.
"""

import logging
import re
import threading
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from orchestrator.clock import Clock
from orchestrator.logging_setup import kv
from orchestrator.mail_client import MailClient, MailError, MailMessage, OutgoingMail
from orchestrator.validation import check_manual_relay

logger = logging.getLogger(__name__)

ALERT_BODY_CHARS = 500
ALERT_INTERVAL = timedelta(hours=1)
MAX_TRACKED_SENDERS = 200
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# --- the commands ----------------------------------------------------------------------------


class CommandName(StrEnum):
    STATUS = "status"
    LOG = "log"
    ON = "on"
    OFF = "off"
    AUTO = "auto"
    SWITCH = "switch"
    RESET = "reset"


CHANGING = frozenset(
    {CommandName.ON, CommandName.OFF, CommandName.AUTO, CommandName.SWITCH, CommandName.RESET}
)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: CommandName
    relay: int | None = None
    seconds: int | None = None

    @property
    def changes_state(self) -> bool:
        """These need the two-factor confirmation (FR-LEG-4); status and log do not."""
        return self.name in CHANGING


@dataclass(frozen=True, slots=True)
class CommandParse:
    """The subject started with a command word; ``error`` says why the rest is not acceptable."""

    command: ParsedCommand | None
    error: str | None = None


def _whole(word: str) -> int | None:
    return int(word) if word.isascii() and word.isdigit() and len(word) <= 6 else None


def parse_command(subject: str) -> CommandParse | None:
    """Recognise a command by the first word of the subject; ``None`` if it is not a command."""
    words = subject.split()
    if not words:
        return None
    try:
        name = CommandName(words[0].lower())
    except ValueError:
        return None
    arguments = words[1:]
    if name is not CommandName.SWITCH:
        if arguments:
            return CommandParse(None, f"'{name.value}' takes no arguments")
        return CommandParse(ParsedCommand(name))
    relay = _whole(arguments[0]) if len(arguments) == 2 else None
    seconds = _whole(arguments[1]) if len(arguments) == 2 else None
    if relay is None or seconds is None:
        return CommandParse(None, "use: switch <relay 2-7> <seconds more than 10>")
    problems = check_manual_relay(relay, seconds).errors
    if problems:
        return CommandParse(None, "; ".join(problems))
    return CommandParse(ParsedCommand(name, relay, seconds))


# --- who may command ------------------------------------------------------------------------


class Decision(StrEnum):
    ACCEPT = "accept"
    IGNORE = "ignore"  # removed from the inbox without a word: our own mail, automatic mail
    REJECT = "reject"  # removed, logged, and reported to the alert address


@dataclass(frozen=True, slots=True)
class Verdict:
    decision: Decision
    reason: str


def classify_message(
    message: MailMessage, authorized: Collection[str], own_address: str
) -> Verdict:
    """Decide what to do with one message (the order of the checks matters)."""
    allowed = {address.strip().lower() for address in authorized}
    own = own_address.strip().lower()
    if message.sender and message.sender == own:
        if message.authenticated is False:
            return Verdict(
                Decision.REJECT,
                "claims to come from this system's own address but failed authentication",
            )
        return Verdict(Decision.IGNORE, "mail from this system's own address")
    if message.automatic:
        return Verdict(Decision.IGNORE, "automatic mail (bounce, bulk or auto-generated)")
    if not message.sender:
        return Verdict(Decision.REJECT, "the From header is not one single valid address")
    if message.sender not in allowed:
        return Verdict(Decision.REJECT, "the sender is not in the authorized list")
    if message.authenticated is False:
        return Verdict(
            Decision.REJECT, "the sender is authorized but Gmail's SPF/DKIM check did not pass"
        )
    return Verdict(Decision.ACCEPT, "authorized sender")


# --- security alerts --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Allowance:
    send: bool
    suppressed: int  # mails from this sender held back since the last alert about them


class AlertLimiter:
    """One alert per sender per hour, and a daily maximum; held-back mails are counted."""

    def __init__(self, clock: Clock, daily_max: int = 20) -> None:
        self._clock = clock
        self._daily_max = daily_max
        self._lock = threading.Lock()
        self._last_sent: dict[str, datetime] = {}  # UTC
        self._held: dict[str, int] = {}
        self._day = clock.now().date()
        self._sent_today = 0

    @property
    def tracked(self) -> int:
        """How many senders are remembered (bounded)."""
        with self._lock:
            return len(self._last_sent) + len(self._held)

    def decide(self, sender: str) -> Allowance:
        with self._lock:
            local = self._clock.now()
            if local.date() != self._day:
                self._day, self._sent_today = local.date(), 0
            now = local.astimezone(UTC)  # UTC: subtracting aware local times ignores DST changes
            last = self._last_sent.get(sender)
            if (last is not None and now - last < ALERT_INTERVAL) or (
                self._sent_today >= self._daily_max
            ):
                self._held[sender] = self._held.get(sender, 0) + 1
                self._forget_old(now)
                return Allowance(False, self._held[sender])
            held = self._held.pop(sender, 0)
            self._last_sent[sender] = now
            self._sent_today += 1
            self._forget_old(now)
            return Allowance(True, held)

    def refund(self, sender: str, held: int) -> None:
        """Undo decide after a failed send, so that the alert is tried again with the next mail."""
        with self._lock:
            self._last_sent.pop(sender, None)
            self._sent_today = max(0, self._sent_today - 1)
            if held:
                self._held[sender] = held

    def _forget_old(self, now: datetime) -> None:
        if len(self._last_sent) + len(self._held) <= MAX_TRACKED_SENDERS:
            return
        for sender, moment in sorted(self._last_sent.items(), key=lambda item: item[1]):
            if len(self._last_sent) + len(self._held) <= MAX_TRACKED_SENDERS:
                break
            if now - moment >= ALERT_INTERVAL:
                del self._last_sent[sender]
                self._held.pop(sender, None)


class AlertOutcome(StrEnum):
    SENT = "sent"
    HELD_BACK = "held_back"  # rate limited
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


def _clean(text: str, limit: int) -> str:
    return _CONTROL.sub(" ", text).replace("\r", "").strip()[:limit]


def _authentication_text(value: bool | None) -> str:
    return "not available" if value is None else "pass" if value else "did not pass"


def build_alert(message: MailMessage, reason: str, held_back: int, now: datetime) -> OutgoingMail:
    """The alert for a rejected message (address and text are shown, nothing is followed)."""
    parsed = parse_command(message.subject)
    shown = _clean(message.sender, 80) or "(no valid address)"
    lines = [
        "A mail was rejected by the boiler Orchestrator.",
        f"Reason: {reason}",
        f"Received: {(message.received or now).isoformat(timespec='seconds')}",
        f"From: {shown}  (display name: {_clean(message.sender_name, 80) or 'none'})",
        f"To: {_clean(message.to, 120) or 'unknown'}",
        f"Subject: {_clean(message.subject, 200)}",
        f"Subject looks like a command: {'yes' if parsed else 'no'}",
        f"Authentication (SPF/DKIM): {_authentication_text(message.authenticated)}",
        f"Mails held back since the last alert about this sender: {held_back}",
        "",
        f"First {ALERT_BODY_CHARS} characters of the text (nothing was opened or followed):",
        "---",
        _clean(message.body, ALERT_BODY_CHARS),
        "---",
        "The mail was removed from the inbox. It was not answered.",
    ]
    return OutgoingMail(
        "", f"Orchestrator security alert: rejected mail from {shown}", "\n".join(lines)
    )


class SecurityAlerts:
    """Sends the rate-limited security alerts to the alert address."""

    def __init__(
        self, client: MailClient, alert_to: str, clock: Clock, *, daily_max: int = 20
    ) -> None:
        self._client = client
        self._to = alert_to.strip()
        self._clock = clock
        self._limiter = AlertLimiter(clock, daily_max)

    def rejected(self, message: MailMessage, reason: str) -> AlertOutcome:
        """Report a rejected message; never raises (a failing alert must not stop the mail loop)."""
        logger.warning(kv("mail_rejected", sender=message.sender or "(none)", reason=reason))
        return self._send(message.sender or "(none)", reason, message)

    def event(self, sender: str, reason: str, message: MailMessage) -> AlertOutcome:
        """Report something else worth an alert about this sender, with the same limits."""
        return self._send(sender or "(none)", reason, message)

    def _send(self, key: str, reason: str, message: MailMessage) -> AlertOutcome:
        if not self._to:
            logger.warning(kv("security_alert_not_configured"))
            return AlertOutcome.NOT_CONFIGURED
        allowance = self._limiter.decide(key)
        if not allowance.send:
            logger.info(kv("security_alert_held_back", sender=key, held=allowance.suppressed))
            return AlertOutcome.HELD_BACK
        alert = build_alert(message, reason, allowance.suppressed, self._clock.now())
        try:
            self._client.send(OutgoingMail(self._to, alert.subject, alert.body))
        except MailError as error:
            logger.error(kv("security_alert_failed", error=str(error)))
            self._limiter.refund(key, allowance.suppressed)
            return AlertOutcome.FAILED
        return AlertOutcome.SENT
