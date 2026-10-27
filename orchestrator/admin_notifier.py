"""E-mail notices to the admin: what happened, once, and once more when it is over (FR-LEG-2).

The notifier has no thread of its own. The workers call it when something changes:

* the status poller reports a change of the boiler connection (:meth:`on_transition`);
* the calendar sync reports every cycle (:meth:`on_sync_report`);
* the mail check reports when Google stops or starts accepting the credentials
  (:meth:`on_mail_auth`).

Each problem is announced once when it starts and once when it is resolved. Down and up notices
for a reboot that was asked for (:class:`orchestrator.reboot_grace.RebootGrace`) are replaced by a
single "back after the requested reboot" notice. A daily maximum protects the inbox. A notice that
cannot be sent is logged and never raises; when Google has revoked the access the "lost" notice
cannot be sent either, which is why the same fact is also shown in the UI and in Home Assistant
(FR-GOOG-7).
"""

import logging
import threading
from datetime import UTC, date, timedelta

from orchestrator.calendar_sync import SyncReport
from orchestrator.clock import Clock
from orchestrator.logging_setup import kv
from orchestrator.mail_client import MailClient, MailError, OutgoingMail
from orchestrator.reboot_grace import RebootGrace
from orchestrator.status_cache import ConnectionState, Transition

logger = logging.getLogger(__name__)

CALENDAR_FAILURES_BEFORE_NOTICE = 3
MAX_PROBLEM_LINES = 20


def _duration_text(span: timedelta) -> str:
    seconds = int(span.total_seconds())
    if seconds < 90:
        return f"{seconds} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{seconds / 3600:.1f} hours"


class AdminNotifier:
    def __init__(
        self,
        client: MailClient,
        admin_email: str,
        clock: Clock,
        *,
        reboot_grace: RebootGrace | None = None,
        daily_max: int = 30,
    ) -> None:
        self._client = client
        self._to = admin_email.strip()
        self._clock = clock
        self._grace = reboot_grace
        self._daily_max = daily_max
        self._lock = threading.Lock()
        self._day: date | None = None
        self._sent_today = 0
        self._capped_notice_sent = False
        self._boiler_down_during_reboot = False
        self._calendar_failures = 0
        self._calendar_alerted = False
        self._auth_lost: set[str] = set()
        self._conditions: set[str] = set()

    # --- conditions that start and end (disk, memory, ...) -------------------------------------

    def condition(
        self,
        key: str,
        active: bool,
        *,
        problem: tuple[str, str],
        resolved: tuple[str, str],
    ) -> None:
        """Mail once when ``active`` turns true and once when it turns false again.

        A notice that could not be sent is tried again at the next call.
        """
        with self._lock:
            was = key in self._conditions
            if active and not was:
                if self._send(key, *problem):
                    self._conditions.add(key)
            elif was and not active:
                self._conditions.discard(key)
                self._send(key, *resolved)

    # --- the service itself --------------------------------------------------------------------

    def started(self, version: str, previous_stop: str, checks: list[str]) -> bool:
        """ "Orchestrator started" with the self-check (OPS-3); returns whether it was sent."""
        with self._lock:
            lines = [f"Version {version} started.", f"Previous stop: {previous_stop}.", "", *checks]
            return self._send("start", "Orchestrator started", "\n".join(lines))

    # --- the boiler Pi -------------------------------------------------------------------------

    def on_transition(self, transition: Transition) -> None:
        """The boiler connection changed (``StatusPoller(on_transition=...)``)."""
        with self._lock:
            if transition.current.unhealthy:
                self._boiler_unhealthy(transition)
            elif transition.previous.unhealthy:
                self._boiler_restored(transition)

    def _boiler_unhealthy(self, transition: Transition) -> None:
        if transition.previous.unhealthy:
            return  # still unhealthy, only the kind of problem changed
        if self._grace is not None and self._grace.suppressed():
            self._boiler_down_during_reboot = True
            logger.info(kv("boiler_down_during_reboot"))
            return
        if self._grace is not None:
            self._grace.finish()  # a reboot asked for long ago explains nothing any more
        self._boiler_down_during_reboot = False
        inactive = transition.current is ConnectionState.INACTIVE
        since = transition.at.astimezone(self._clock.tz).isoformat(timespec="seconds")
        what = (
            "The boiler service on the boiler Pi is not active."
            if inactive
            else ("The boiler Pi is not responding.")
        )
        self._send(
            "boiler Pi" if not inactive else "boiler service",
            "Boiler problem: " + ("service not active" if inactive else "Pi not responding"),
            f"{what}\nSince: {since}\n"
            f"Last error: {transition.error or 'none'}\n\n"
            "The schedules in force on the boiler Pi keep working. You get another mail when it is "
            "back.",
        )

    def _boiler_restored(self, transition: Transition) -> None:
        took = self._grace.finish() if self._grace is not None else None
        via_reboot = self._boiler_down_during_reboot
        self._boiler_down_during_reboot = False
        away = f" It was away for {_duration_text(transition.outage)}." if transition.outage else ""
        if via_reboot and took is not None:
            self._send(
                "boiler Pi",
                "Boiler Pi is back after the requested reboot",
                f"The boiler Pi answers again, {_duration_text(took)} after the reboot was "
                "requested.",
            )
            return
        self._send(
            "boiler Pi",
            "Boiler Pi is back",
            f"The boiler Pi answers again.{away}",
        )

    # --- the calendar --------------------------------------------------------------------------

    def on_sync_report(self, report: SyncReport) -> None:
        """A calendar cycle finished (``CalendarSync(on_report=...)``)."""
        with self._lock:
            self._note_auth("calendar", report.auth_lost)
            if report.auth_lost:
                return  # its own notice; not also "cannot be read"
            if not report.ok:
                self._calendar_failed(report)
                return
            self._calendar_read(report)

    def _calendar_failed(self, report: SyncReport) -> None:
        self._calendar_failures += 1
        if (
            self._calendar_failures >= CALENDAR_FAILURES_BEFORE_NOTICE
            and not self._calendar_alerted
        ):
            self._calendar_alerted = self._send(
                "calendar",
                "Calendar cannot be read",
                f"The Google calendar could not be read {self._calendar_failures} times in a row.\n"
                f"Last error: {report.error or 'unknown'}\n\n"
                "The last good schedules stay in force. You get another mail when it works again.",
            )

    def _calendar_read(self, report: SyncReport) -> None:
        self._calendar_failures = 0
        if self._calendar_alerted:
            self._calendar_alerted = False
            self._send(
                "calendar", "Calendar can be read again", "The Google calendar is readable again."
            )
        if report.diff and report.file_changed and (report.boiler_changed or report.timer_changed):
            lines = [report.diff.replace("\r\n", "\n")]
            if report.problems:
                shown = report.problems[:MAX_PROBLEM_LINES]
                lines += ["", "Entries that were skipped:", *(f"  {p}" for p in shown)]
                if len(report.problems) > len(shown):
                    lines.append(f"  ... and {len(report.problems) - len(shown)} more")
            self._send("calendar changes", "Boiler calendar changed", "\n".join(lines))

    # --- Google authorization ------------------------------------------------------------------

    def on_mail_auth(self, lost: bool) -> None:
        """The mail check found Google refusing (or accepting again) the credentials."""
        with self._lock:
            self._note_auth("mail", lost)

    def _note_auth(self, source: str, lost: bool) -> None:
        before = bool(self._auth_lost)
        if lost:
            self._auth_lost.add(source)
        else:
            self._auth_lost.discard(source)
        now_lost = bool(self._auth_lost)
        if now_lost and not before:
            # likely cannot be delivered when mail is the one that lost access; that is expected
            self._send(
                "Google authorization",
                "Google authorization needed",
                f"Google no longer accepts the access of the Orchestrator ({source}). Calendar and "
                "mail commands do not work until it is authorized again:\n"
                "  python -m orchestrator.auth\n"
                "The last good schedules stay in force.",
            )
        elif before and not now_lost:
            self._send(
                "Google authorization", "Google access works again", "Authorization is fine again."
            )

    # --- sending -------------------------------------------------------------------------------

    def _send(self, topic: str, subject: str, body: str) -> bool:
        if not self._to:
            logger.info(kv("admin_notice_not_configured", topic=topic))
            return False
        today = self._clock.now().astimezone(UTC).date()
        if today != self._day:
            self._day, self._sent_today, self._capped_notice_sent = today, 0, False
        if self._sent_today >= self._daily_max:
            logger.warning(kv("admin_notice_over_daily_maximum", topic=topic))
            if self._capped_notice_sent:
                return False
            self._capped_notice_sent = True
            subject, body = "Further notices held back today", (
                f"{self._daily_max} notices were sent today; the rest are only in the log."
            )
        self._sent_today += 1
        stamp = self._clock.now().isoformat(timespec="seconds")
        try:
            self._client.send(
                OutgoingMail(self._to, f"Orchestrator: {subject}", f"{body}\n\n{stamp}")
            )
        except MailError as error:
            self._sent_today -= 1  # a notice that was not delivered does not use up the day
            logger.error(kv("admin_notice_failed", topic=topic, error=str(error)))
            return False
        logger.info(kv("admin_notice_sent", topic=topic))
        return True
