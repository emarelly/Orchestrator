"""Diagnostics of the service itself: resource alerts, the hourly heartbeat line, MQTT values.

* OPS-10: free disk space below 10 %, memory above 150 MB for 10 minutes, CPU temperature above
  80 degrees, clock not synchronized after 10 minutes, a schedule file waiting to be pushed for
  more than 15 minutes. Each problem is mailed to the admin once when it starts and once when it
  is over. A value that cannot be read is never taken as a problem.
* LOG-11 / OPS-11: one INFO line an hour with the key figures (it also feeds the Pi 3 performance
  check, NFR-1a).
* OPS-9: the values that Home Assistant shows as diagnostic entities.
"""

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orchestrator.admin_notifier import AdminNotifier
from orchestrator.calendar_sync import SyncReport
from orchestrator.clock import Clock
from orchestrator.logging_setup import kv
from orchestrator.push_queue import PushStatus
from orchestrator.supervisor import Health, HealthReport

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Limits:
    disk_free_min_percent: float = 10.0
    temperature_max_c: float = 80.0
    temperature_clear_c: float = 75.0  # the alert ends only below this, so it does not flap
    memory_max_mb: float = 150.0
    memory_for_s: float = 600.0
    clock_unsynchronized_for_s: float = 600.0
    push_pending_for_s: float = 900.0
    heartbeat_every_s: float = 3600.0


class Diagnostics:
    def __init__(
        self,
        clock: Clock,
        notifier: AdminNotifier,
        *,
        facts: Callable[[], dict[str, Any]],
        push_statuses: Callable[[], list[PushStatus]],
        health: Callable[[], HealthReport],
        sync_report: Callable[[], SyncReport | None],
        version: str,
        limits: Limits | None = None,
        interval_s: float = 60.0,
    ) -> None:
        self._clock = clock
        self._notifier = notifier
        self._facts = facts
        self._push_statuses = push_statuses
        self._health = health
        self._sync_report = sync_report
        self._version = version
        self._limits = limits or Limits()
        self._interval = interval_s
        self._memory_high_since: float | None = None
        self._clock_bad_since: float | None = None
        self._push_pending_since: dict[str, float] = {}
        self._temperature_alert = False
        self._last_heartbeat: float | None = None
        self.last_cycle: float | None = None

    # --- the alerts ------------------------------------------------------------------------------

    def check(self) -> None:
        now = self._clock.monotonic()
        facts = self._facts()
        self._check_disk(facts.get("disk_free_percent"))
        self._check_temperature(facts.get("cpu_temp_c"))
        self._check_memory(facts.get("memory_rss_mb"), now)
        self._check_clock(facts.get("clock_synchronized"), now)
        self._check_pushes(now)

    def _check_disk(self, free: float | None) -> None:
        if free is None:
            return
        low = free < self._limits.disk_free_min_percent
        self._notifier.condition(
            "disk",
            low,
            problem=(
                "Disk almost full",
                f"Only {free:.1f}% of the disk is free (limit "
                f"{self._limits.disk_free_min_percent:.0f}%). Logs and schedule files need room.",
            ),
            resolved=("Disk space is fine again", f"{free:.1f}% of the disk is free."),
        )

    def _check_temperature(self, temperature: float | None) -> None:
        if temperature is None:
            return
        limits = self._limits
        if temperature > limits.temperature_max_c:
            self._temperature_alert = True
        elif temperature < limits.temperature_clear_c:
            self._temperature_alert = False
        self._notifier.condition(
            "temperature",
            self._temperature_alert,
            problem=(
                "Pi is too hot",
                f"The CPU is at {temperature:.1f} degrees (limit "
                f"{limits.temperature_max_c:.0f}).",
            ),
            resolved=("Pi has cooled down", f"The CPU is at {temperature:.1f} degrees."),
        )

    def _check_memory(self, used: float | None, now: float) -> None:
        if used is None:
            return
        if used > self._limits.memory_max_mb:
            if self._memory_high_since is None:
                self._memory_high_since = now
        else:
            self._memory_high_since = None
        long_enough = (
            self._memory_high_since is not None
            and now - self._memory_high_since >= self._limits.memory_for_s
        )
        self._notifier.condition(
            "memory",
            long_enough,
            problem=(
                "Memory use is high",
                f"The service uses {used:.0f} MB (limit "
                f"{self._limits.memory_max_mb:.0f} MB for {self._limits.memory_for_s / 60:.0f} "
                "minutes). It is restarted by systemd at the hard limit.",
            ),
            resolved=("Memory use is normal again", f"The service uses {used:.0f} MB."),
        )

    def _check_clock(self, synchronized: bool | None, now: float) -> None:
        if synchronized is None:
            return
        if synchronized:
            self._clock_bad_since = None
        elif self._clock_bad_since is None:
            self._clock_bad_since = now
        long_enough = (
            self._clock_bad_since is not None
            and now - self._clock_bad_since >= self._limits.clock_unsynchronized_for_s
        )
        self._notifier.condition(
            "clock",
            long_enough,
            problem=(
                "Clock is not synchronized",
                "The Pi's clock has not been synchronized for "
                f"{self._limits.clock_unsynchronized_for_s / 60:.0f} minutes. No schedule or "
                "manual file is sent to the boiler Pi until it is.",
            ),
            resolved=("Clock is synchronized", "The Pi's clock is synchronized again."),
        )

    def _check_pushes(self, now: float) -> None:
        waiting: list[str] = []
        for status in self._push_statuses():
            kind = status.kind.value
            if status.pending:
                started = self._push_pending_since.setdefault(kind, now)
                if now - started >= self._limits.push_pending_for_s:
                    waiting.append(f"{kind} ({status.last_error or 'no error reported'})")
            else:
                self._push_pending_since.pop(kind, None)
        self._notifier.condition(
            "push",
            bool(waiting),
            problem=(
                "Schedule file not delivered",
                "These schedule files have waited more than "
                f"{self._limits.push_pending_for_s / 60:.0f} minutes to be sent to the boiler "
                "Pi: " + ", ".join(waiting),
            ),
            resolved=("Schedule files delivered", "Every schedule file is on the boiler Pi again."),
        )

    # --- the heartbeat line ----------------------------------------------------------------------

    def maybe_heartbeat(self) -> bool:
        """Log the hourly line if it is due; returns whether it was logged."""
        now = self._clock.monotonic()
        if (
            self._last_heartbeat is not None
            and now - self._last_heartbeat < self._limits.heartbeat_every_s
        ):
            return False
        if self._last_heartbeat is None:
            self._last_heartbeat = now  # the first line comes an hour after the start
            return False
        self._last_heartbeat = now
        logger.info(kv("heartbeat", **self.figures()))
        return True

    def figures(self) -> dict[str, Any]:
        """The key figures: used by the heartbeat line and the performance check."""
        report = self._health()
        facts = self._facts()
        figures: dict[str, Any] = {
            "version": self._version,
            "status": report.status.value,
            "uptime_s": report.uptime_s,
            "rss_mb": facts.get("memory_rss_mb"),
            "cpu_temp_c": facts.get("cpu_temp_c"),
            "disk_free_pct": facts.get("disk_free_percent"),
            "clock_synchronized": facts.get("clock_synchronized"),
            "push_pending": sum(1 for s in self._push_statuses() if s.pending),
            "failed": ",".join(c.name for c in report.components if c.state is Health.FAILED)
            or None,
            "degraded": ",".join(c.name for c in report.components if c.state is Health.DEGRADED)
            or None,
        }
        for component in report.components:
            key = "cycle_age_" + re.sub(r"[^a-z0-9]+", "_", component.name.lower())
            figures[key] = component.seconds_since_cycle
        return figures

    # --- Home Assistant -----------------------------------------------------------------------

    def mqtt_values(self) -> dict[str, Any]:
        """Values for the diagnostic entities (rounded so that they do not change every second)."""
        report = self._health()
        facts = self._facts()
        values: dict[str, Any] = {
            "version": self._version,
            "uptime_min": report.uptime_s // 60,
            "pending_pushes": sum(1 for s in self._push_statuses() if s.pending),
            "failed_components": sum(1 for c in report.components if c.state is Health.FAILED),
        }
        memory, temperature = facts.get("memory_rss_mb"), facts.get("cpu_temp_c")
        if memory is not None:
            values["memory_mb"] = round(memory)
        if temperature is not None:
            values["cpu_temp_c"] = round(temperature)
        sync = self._sync_report()
        if sync is not None and sync.ok:
            values["last_calendar_sync"] = _iso(sync.at)
        return values

    # --- thread -------------------------------------------------------------------------------

    def step(self) -> None:
        try:
            self.check()
            self.maybe_heartbeat()
        except Exception:  # CS-11: a diagnostics bug must never end the loop
            logger.exception(kv("diagnostics_failed"))
        self.last_cycle = self._clock.monotonic()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.step()
            stop.wait(self._interval)

    def start(self, stop: threading.Event) -> threading.Thread:
        thread = threading.Thread(target=self.run, args=(stop,), name="diagnostics", daemon=True)
        thread.start()
        return thread


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
