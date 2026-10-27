"""Boiler status cache and poller (FR-STA-1..5, CS-11, CS-12).

One worker thread (:class:`StatusPoller`) asks the boiler Pi for its status every few seconds and
writes the outcome into a :class:`StatusCache`; everything else (web handlers, MQTT, e-mail) only
reads immutable snapshots from the cache, so the load on the boiler Pi does not grow with the number
of open pages (NFR-1a).

Rules:

* **Nothing is invented.** After a failed poll the cache keeps the *last good* reading and says how
  old it is; it never turns a failure into zeros or a made-up status.
* **Stale is judged by the reading itself** (its ``Date``), not by when we fetched it: a Pi that
  answers with an old reading is stale even though it answers (FR-STA-3).
* **Down needs several failures in a row** (``down_after_failures``), so a single timeout does not
  raise an alarm. Going down and coming back are each reported **once** as a :class:`Transition`.
* **A boiler service that answers but reports it is not active** (``ActiveYesNo`` = No) is its own
  state, ``INACTIVE``, and counts as unhealthy, like the legacy "manager is not working" alert.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from orchestrator.boiler_client import BoilerError
from orchestrator.clock import Clock
from orchestrator.config import BoilerConfig
from orchestrator.logging_setup import kv
from orchestrator.models import BoilerStatus

logger = logging.getLogger(__name__)


class ConnectionState(StrEnum):
    UNKNOWN = "unknown"  # nothing has been learned yet
    UP = "up"  # answers, and the boiler service is active
    INACTIVE = "inactive"  # answers, but ActiveYesNo says the boiler service is not active
    DOWN = "down"  # does not answer, or answers with something unusable, repeatedly

    @property
    def unhealthy(self) -> bool:
        return self in (ConnectionState.INACTIVE, ConnectionState.DOWN)


@dataclass(frozen=True, slots=True)
class Transition:
    """A change of connection state, reported once."""

    previous: ConnectionState
    current: ConnectionState
    at: datetime
    outage: timedelta | None  # how long it was unhealthy, set when returning to UP
    error: str | None  # the last error, set when going unhealthy


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """What the rest of the program may know about the boiler status (immutable)."""

    state: ConnectionState = ConnectionState.UNKNOWN
    status: BoilerStatus | None = None  # the last good reading, if any
    fetched_at: datetime | None = None  # when that reading was fetched (aware)
    last_attempt_at: datetime | None = None
    error: str | None = None  # the last error, cleared by a good reading
    consecutive_failures: int = 0
    unhealthy_since: datetime | None = None

    def is_stale(self, local_now: datetime, limit: timedelta) -> bool:
        """True if there is no reading or the reading itself is older than ``limit``."""
        return self.status is None or self.status.age(local_now) > limit


class StatusCache:
    """Thread-safe holder of the latest :class:`StatusSnapshot`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = StatusSnapshot()

    def snapshot(self) -> StatusSnapshot:
        with self._lock:
            return self._snapshot

    def fresh_status(self, now: datetime, stale_after: timedelta) -> BoilerStatus | None:
        """Return the reading if the boiler Pi is not down and the reading is not stale.

        This is what an action such as manual On must use: with ``None`` it has to refuse
        instead of guessing.
        """
        snapshot = self.snapshot()
        if snapshot.state in (ConnectionState.DOWN, ConnectionState.UNKNOWN):
            return None
        if snapshot.is_stale(now, stale_after):
            return None
        return snapshot.status

    def record_success(self, status: BoilerStatus, now: datetime) -> Transition | None:
        """Store a good reading; returns a transition if the connection state changed."""
        state = ConnectionState.UP if status.active else ConnectionState.INACTIVE
        with self._lock:
            before = self._snapshot
            since = before.unhealthy_since if state.unhealthy else None
            if state.unhealthy and not before.state.unhealthy:
                since = now
            self._snapshot = StatusSnapshot(state, status, now, now, None, 0, since)
            return _transition(
                before, state, now, None if not state.unhealthy else "boiler service inactive"
            )

    def record_failure(self, error: str, now: datetime, down_after: int) -> Transition | None:
        """Count a failed poll; goes DOWN after ``down_after`` failures in a row."""
        with self._lock:
            before = self._snapshot
            failures = before.consecutive_failures + 1
            state = before.state
            since = before.unhealthy_since
            if failures >= down_after and state is not ConnectionState.DOWN:
                state = ConnectionState.DOWN
                since = since or now
            self._snapshot = replace(
                before,
                state=state,
                last_attempt_at=now,
                error=error,
                consecutive_failures=failures,
                unhealthy_since=since,
            )
            return _transition(before, state, now, error)


def _transition(
    before: StatusSnapshot, state: ConnectionState, now: datetime, error: str | None
) -> Transition | None:
    if state is before.state:
        return None
    outage = None
    if state is ConnectionState.UP and before.unhealthy_since is not None:
        outage = now - before.unhealthy_since
    return Transition(before.state, state, now, outage, error if state.unhealthy else None)


class StatusSource(Protocol):
    """What the poller needs from the boiler client."""

    def get_status(self) -> BoilerStatus: ...


class StatusPoller:
    """Polls the boiler Pi and feeds the cache; run it in one worker thread."""

    def __init__(
        self,
        source: StatusSource,
        cache: StatusCache,
        clock: Clock,
        config: BoilerConfig,
        *,
        interval_s: float | None = None,
        on_transition: Callable[[Transition], None] | None = None,
    ) -> None:
        self._source = source
        self._cache = cache
        self._clock = clock
        self._down_after = config.down_after_failures
        self._interval = config.poll_interval_s if interval_s is None else interval_s
        self._on_transition = on_transition
        self._last_cycle: float | None = None

    @property
    def last_cycle(self) -> float | None:
        """Monotonic time of the last finished poll (for the supervisor heartbeat)."""
        return self._last_cycle

    def poll_once(self) -> Transition | None:
        """Ask the boiler Pi once and update the cache."""
        now = self._clock.now().astimezone(
            UTC
        )  # UTC: outage lengths must not follow the wall clock
        try:
            status = self._source.get_status()
        except BoilerError as error:
            logger.debug(kv("boiler_poll_failed", error=error))
            transition = self._cache.record_failure(str(error), now, self._down_after)
        else:
            transition = self._cache.record_success(status, now)
        self._last_cycle = self._clock.monotonic()
        if transition is not None:
            self._announce(transition)
        return transition

    def run(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set; an unexpected error is logged and the loop carries on."""
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:  # CS-11: one bad cycle must never end the loop
                logger.exception(kv("status_poll_crashed"))
            stop.wait(self._interval)

    def start(self, stop: threading.Event) -> threading.Thread:
        """Start the polling loop in a daemon thread."""
        thread = threading.Thread(target=self.run, args=(stop,), name="status-poller", daemon=True)
        thread.start()
        return thread

    def _announce(self, transition: Transition) -> None:
        previous, current = transition.previous, transition.current
        if current is ConnectionState.DOWN:
            logger.warning(kv("boiler_down", previous=previous, error=transition.error))
        elif current is ConnectionState.INACTIVE:
            logger.warning(kv("boiler_inactive", previous=previous))
        elif previous is ConnectionState.UNKNOWN:
            logger.info(kv("boiler_first_reading"))
        else:
            seconds = round(transition.outage.total_seconds()) if transition.outage else None
            logger.info(kv("boiler_up", previous=previous, outage_s=seconds))
        if self._on_transition is not None:
            try:
                self._on_transition(transition)
            except Exception:
                logger.exception(kv("transition_handler_failed", current=current))
