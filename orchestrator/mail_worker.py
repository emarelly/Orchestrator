"""The mail check loop: read the inbox, apply the sender checks, run the commands (FR-LEG-1/3).

Every message that was looked at is removed from the inbox afterwards, whatever happened to it, so
that a bad message can never be processed twice or block the ones behind it.
"""

import logging
import threading
import time as wall_time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from orchestrator.logging_setup import correlation, kv
from orchestrator.mail_client import (
    MailAuthError,
    MailClient,
    MailError,
    MailMessage,
)
from orchestrator.mail_commands import MailCommands
from orchestrator.mail_gate import Decision, SecurityAlerts, classify_message

logger = logging.getLogger(__name__)

BATCH = 20
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class MailCycle:
    """What one pass over the inbox did (for the logs and the supervisor)."""

    accepted: int = 0
    ignored: int = 0
    rejected: int = 0
    errors: int = 0
    more: bool = False  # more mail is waiting; the next pass should come soon


class MailWorker:
    def __init__(
        self,
        client: MailClient,
        commands: MailCommands,
        alerts: SecurityAlerts,
        authorized: Collection[str],
        *,
        interval_s: float = 30.0,
        on_auth_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._on_auth_change = on_auth_change
        self._client = client
        self._commands = commands
        self._alerts = alerts
        self._authorized = tuple(authorized)
        self._interval = interval_s
        self._lock = threading.Lock()
        self._auth_lost = False
        self.last_cycle: float | None = None

    @property
    def auth_lost(self) -> bool:
        """Google refused the credentials in the last pass (FR-GOOG-7)."""
        return self._auth_lost

    def process_inbox(self) -> MailCycle:
        """One pass over the inbox; mail trouble is logged and ends the pass, it never raises."""
        with self._lock:
            try:
                own = self._client.own_address()
                read = self._client.list_inbox(BATCH)
            except MailAuthError as error:
                if not self._auth_lost:
                    logger.error(kv("mail_auth_lost", error=str(error)))
                    self._set_auth_lost(True)
                return MailCycle(errors=1)
            except MailError as error:
                logger.warning(kv("mail_read_failed", error=str(error)))
                return MailCycle(errors=1)
            if self._auth_lost:
                logger.info(kv("mail_auth_restored"))
                self._set_auth_lost(False)
            counts = {Decision.ACCEPT: 0, Decision.IGNORE: 0, Decision.REJECT: 0}
            errors = 0
            for message in sorted(read.messages, key=lambda m: m.received or _EPOCH):
                with correlation():
                    try:
                        decision = self._one(message, own)
                        counts[decision] += 1
                    except Exception:  # one bad message must not stop the others
                        logger.exception(kv("mail_message_failed"))
                        errors += 1
                    self._remove(message)
            return MailCycle(
                counts[Decision.ACCEPT],
                counts[Decision.IGNORE],
                counts[Decision.REJECT],
                errors,
                read.more,
            )

    def _set_auth_lost(self, lost: bool) -> None:
        self._auth_lost = lost
        if self._on_auth_change is not None:
            try:
                self._on_auth_change(lost)
            except Exception:  # a failing listener must not stop the mail check
                logger.exception(kv("mail_auth_listener_failed"))

    def _one(self, message: MailMessage, own: str) -> Decision:
        verdict = classify_message(message, self._authorized, own)
        if verdict.decision is Decision.REJECT:
            self._alerts.rejected(message, verdict.reason)
        elif verdict.decision is Decision.IGNORE:
            logger.info(kv("mail_ignored", reason=verdict.reason))
        else:
            self._commands.handle(message)
        return verdict.decision

    def _remove(self, message: MailMessage) -> None:
        try:
            self._client.remove(message.message_id)
        except MailError as error:
            logger.error(kv("mail_remove_failed", error=str(error)))

    # --- thread --------------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            cycle = self.process_inbox()
            self.last_cycle = wall_time.monotonic()
            stop.wait(1.0 if cycle.more else self._interval)

    def start(self, stop: threading.Event) -> threading.Thread:
        thread = threading.Thread(target=self.run, args=(stop,), name="mail", daemon=True)
        thread.start()
        return thread
