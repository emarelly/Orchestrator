"""Quiet period after a requested reboot of the boiler Pi (FR-LEG-3a).

While the Pi restarts the status poll fails; those failures are expected and must not send the
"boiler down" alert. :meth:`RebootGrace.begin` starts the period, :meth:`suppressed` is what the
alert code asks, and :meth:`finish` is called when the Pi answers again and returns how long it was
away, so that the "back up" mail can say that it was the requested reboot.
"""

import threading
from datetime import UTC, datetime, timedelta

from orchestrator.clock import Clock


class RebootGrace:
    def __init__(self, clock: Clock, grace: timedelta = timedelta(minutes=5)) -> None:
        self._clock = clock
        self._grace = grace
        self._lock = threading.Lock()
        self._started: datetime | None = None  # UTC

    def begin(self) -> None:
        with self._lock:
            self._started = self._clock.now().astimezone(UTC)

    def suppressed(self) -> bool:
        """True while a requested reboot may still explain a missing boiler Pi."""
        with self._lock:
            return self._started is not None and self._now() - self._started < self._grace

    def pending(self) -> bool:
        """True from the request until the Pi has been seen again (even after the grace ends)."""
        with self._lock:
            return self._started is not None

    def finish(self) -> timedelta | None:
        """The Pi answers again: return how long the reboot took, or ``None`` if none was asked."""
        with self._lock:
            if self._started is None:
                return None
            took = self._now() - self._started
            self._started = None
            return took

    def _now(self) -> datetime:
        return self._clock.now().astimezone(UTC)
