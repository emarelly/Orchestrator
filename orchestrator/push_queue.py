"""Pushing the schedules to the boiler Pi (FR-PUSH-1..5, CS-10, CS-14, OPS-5).

The queue keeps the *desired* ``Cal.json`` and ``TimeCal.json`` in the data folder and remembers the
SHA-256 of what the boiler Pi last accepted. A schedule is **pending** whenever the two differ, so:

* only a schedule that changed is sent, and only once it differs from what was last accepted;
* a schedule counts as pushed only after the boiler Pi answered 2xx (never before), so a crash or a
  failed upload leaves it pending and it is sent again, also after a restart;
* failures are retried with exponential backoff and jitter, and uploads are idempotent;
* nothing is sent while the gate says the boiler Pi is down, and nothing counts as pushed in
  dry-run mode.

Manual commands (On / Off / Auto, relay runs) are *not* queued here: they are one-shot and are never
retried automatically (CS-10).
"""

import json
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from orchestrator.boiler_client import BoilerError, UploadResult
from orchestrator.clock import Clock
from orchestrator.files import sha256_hex, write_atomic
from orchestrator.logging_setup import audit, kv
from orchestrator.models import BoilerSchedule, TimerSchedule
from orchestrator.schedule_files import serialize_boiler_schedule, serialize_timer_schedule
from orchestrator.validation import check_boiler_schedule, check_timer_schedule

logger = logging.getLogger(__name__)

STATE_FILENAME = "push_state.json"


class ScheduleKind(StrEnum):
    BOILER = "boiler"
    TIMER = "timer"


FILENAMES = {ScheduleKind.BOILER: "Cal.json", ScheduleKind.TIMER: "TimeCal.json"}


class Outcome(StrEnum):
    PUSHED = "pushed"
    FAILED = "failed"
    DRY_RUN = "dry_run"  # built and logged, nothing sent
    SKIPPED = "skipped"  # not due, not pending, gate closed or no local file


class ScheduleUploader(Protocol):
    """What the queue needs from the boiler client."""

    def upload_boiler_schedule(self, content: bytes) -> UploadResult: ...

    def upload_timer_schedule(self, content: bytes) -> UploadResult: ...


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential backoff with jitter: ``base * factor**(attempt-1)``, capped, +-``jitter``."""

    base_s: float = 10.0
    factor: float = 2.0
    max_s: float = 300.0
    jitter: float = 0.2

    def delay(self, attempt: int, random_value: float) -> timedelta:
        """Return the wait after the ``attempt``-th failure; ``random_value`` is in [0, 1)."""
        seconds = min(self.max_s, self.base_s * self.factor ** (attempt - 1))
        seconds *= 1 - self.jitter + 2 * self.jitter * random_value
        return timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class PushStatus:
    """The state of one schedule, for the UI and the monitoring (immutable)."""

    kind: ScheduleKind
    has_local_file: bool
    pending: bool
    last_pushed_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str | None
    attempts: int
    next_attempt_at: datetime | None


@dataclass(frozen=True, slots=True)
class PushResult:
    kind: ScheduleKind
    outcome: Outcome
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    changed_locally: bool  # the local file was rewritten
    pending: bool  # it now differs from what the boiler Pi last accepted


@dataclass(slots=True)
class _Entry:
    content: bytes | None = None
    pushed_sha: str | None = None
    pushed_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    attempts: int = 0
    next_attempt_at: datetime | None = None
    force: bool = False

    @property
    def pending(self) -> bool:
        if self.content is None:
            return False
        return self.force or sha256_hex(self.content) != self.pushed_sha


class PushQueue:
    """Keeps the desired schedules and delivers them to the boiler Pi."""

    def __init__(
        self,
        uploader: ScheduleUploader,
        directory: Path,
        clock: Clock,
        *,
        gate: Callable[[], bool] = lambda: True,
        backoff: BackoffPolicy | None = None,
        jitter_source: Callable[[], float] = random.random,
        poll_interval_s: float = 5.0,
    ) -> None:
        self._uploader = uploader
        self._directory = directory
        self._clock = clock
        self._gate = gate
        self._backoff = backoff or BackoffPolicy()
        self._jitter_source = jitter_source
        self._poll_interval = poll_interval_s
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._entries = {kind: _Entry() for kind in ScheduleKind}
        self.last_cycle: float | None = None  # monotonic time of the last finished cycle
        self._load()

    # --- loading and saving ------------------------------------------------------------------

    def _path(self, kind: ScheduleKind) -> Path:
        return self._directory / FILENAMES[kind]

    def _load(self) -> None:
        """Read the local files and the record of what was last pushed (OPS-5)."""
        state: dict[str, dict[str, str]] = {}
        try:
            raw = json.loads((self._directory / STATE_FILENAME).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning(kv("push_state_unreadable", action="everything will be pushed again"))
        for kind, entry in self._entries.items():
            try:
                entry.content = self._path(kind).read_bytes()
            except FileNotFoundError:
                entry.content = None
            recorded = state.get(kind.value, {})
            entry.pushed_sha = recorded.get("sha256")
            pushed_at = recorded.get("pushed_at")
            entry.pushed_at = datetime.fromisoformat(pushed_at) if pushed_at else None

    def _save_state(self) -> None:
        state = {
            kind.value: {"sha256": entry.pushed_sha, "pushed_at": entry.pushed_at.isoformat()}
            for kind, entry in self._entries.items()
            if entry.pushed_sha and entry.pushed_at
        }
        text = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
        write_atomic(self._directory / STATE_FILENAME, text, keep_backup=False)

    # --- submitting a schedule ---------------------------------------------------------------

    def set_boiler_schedule(self, schedule: BoilerSchedule) -> SubmitResult:
        """Validate, serialize and store a new boiler schedule; it is pushed by the next cycle."""
        check_boiler_schedule(schedule).raise_if_errors()
        return self._submit(ScheduleKind.BOILER, serialize_boiler_schedule(schedule))

    def set_timer_schedule(self, schedule: TimerSchedule) -> SubmitResult:
        """Validate, serialize and store a new timer schedule; it is pushed by the next cycle."""
        check_timer_schedule(schedule).raise_if_errors()
        return self._submit(ScheduleKind.TIMER, serialize_timer_schedule(schedule))

    def _submit(self, kind: ScheduleKind, content: bytes) -> SubmitResult:
        with self._lock:
            entry = self._entries[kind]
            changed = write_atomic(self._path(kind), content)
            if changed:
                entry.next_attempt_at = None  # new content: try immediately
                entry.attempts = 0
                entry.last_error = None
            entry.content = content
            pending = entry.pending
        if pending:
            self._wake.set()
        return SubmitResult(changed_locally=changed, pending=pending)

    # --- pushing -----------------------------------------------------------------------------

    def process(self) -> list[PushResult]:
        """Push every pending schedule that is due; returns what happened to each."""
        return [self._try_push(kind, force=False) for kind in ScheduleKind]

    def push_now(self) -> list[PushResult]:
        """Send both schedules again, changed or not and without waiting (FR-PUSH-5)."""
        with self._lock:
            for entry in self._entries.values():
                if entry.content is not None:
                    entry.force = True
                    entry.next_attempt_at = None
        return [self._try_push(kind, force=True) for kind in ScheduleKind]

    def _try_push(self, kind: ScheduleKind, *, force: bool) -> PushResult:
        now = self._clock.now().astimezone(UTC)  # UTC: retry times must not follow the wall clock
        with self._lock:
            entry = self._entries[kind]
            content = entry.content
            if content is None or not entry.pending:
                return PushResult(kind, Outcome.SKIPPED)
            if entry.next_attempt_at is not None and now < entry.next_attempt_at and not force:
                return PushResult(kind, Outcome.SKIPPED)
        if not self._gate():
            logger.debug(kv("push_waiting", kind=kind, reason="boiler Pi not available"))
            return PushResult(kind, Outcome.SKIPPED)
        try:
            if kind is ScheduleKind.BOILER:
                result = self._uploader.upload_boiler_schedule(content)
            else:
                result = self._uploader.upload_timer_schedule(content)
        except BoilerError as error:
            return self._failed(kind, str(error), now)
        return self._succeeded(kind, content, result, now)

    def _failed(self, kind: ScheduleKind, error: str, now: datetime) -> PushResult:
        with self._lock:
            entry = self._entries[kind]
            entry.attempts += 1
            entry.last_attempt_at = now
            entry.last_error = error
            entry.force = False
            wait = self._backoff.delay(entry.attempts, self._jitter_source())
            entry.next_attempt_at = now + wait
            attempts = entry.attempts
        log = logger.warning if attempts == 1 else logger.debug
        log(
            kv(
                "schedule_push_failed",
                kind=kind,
                attempts=attempts,
                retry_in_s=round(wait.total_seconds()),
                error=error,
            )
        )
        return PushResult(kind, Outcome.FAILED, error)

    def _succeeded(
        self, kind: ScheduleKind, content: bytes, result: UploadResult, now: datetime
    ) -> PushResult:
        with self._lock:
            entry = self._entries[kind]
            entry.last_attempt_at = now
            entry.force = False
            if not result.sent:  # dry run: logged by the client, nothing was delivered
                entry.next_attempt_at = now + timedelta(seconds=self._poll_interval)
                return PushResult(kind, Outcome.DRY_RUN)
            attempts = entry.attempts
            entry.pushed_sha = sha256_hex(content)
            entry.pushed_at = now
            entry.last_error = None
            entry.attempts = 0
            entry.next_attempt_at = None
            self._save_state()
        logger.info(
            kv("schedule_pushed", kind=kind, size=len(content), after_attempts=attempts + 1)
        )
        audit("schedule_pushed", kind=kind, who="orchestrator", result="ok", size=len(content))
        return PushResult(kind, Outcome.PUSHED)

    # --- reporting ---------------------------------------------------------------------------

    def statuses(self) -> list[PushStatus]:
        """Return the state of both schedules (for the UI, MQTT and the monitoring)."""
        with self._lock:
            return [
                PushStatus(
                    kind,
                    entry.content is not None,
                    entry.pending,
                    entry.pushed_at,
                    entry.last_attempt_at,
                    entry.last_error,
                    entry.attempts,
                    entry.next_attempt_at,
                )
                for kind, entry in self._entries.items()
            ]

    # --- worker thread -----------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Push when something is pending until ``stop`` is set (wakes up on every submit)."""
        while not stop.is_set():
            try:
                self.process()
            except Exception:  # CS-11: one bad cycle must never end the loop
                logger.exception(kv("push_cycle_crashed"))
            self.last_cycle = self._clock.monotonic()
            self._wake.wait(self._poll_interval)
            self._wake.clear()

    def start(self, stop: threading.Event) -> threading.Thread:
        """Start the push loop in a daemon thread."""
        thread = threading.Thread(target=self.run, args=(stop,), name="push-queue", daemon=True)
        thread.start()
        return thread

    def wake(self) -> None:
        """Make a sleeping loop run a cycle now (also used to stop it quickly)."""
        self._wake.set()
