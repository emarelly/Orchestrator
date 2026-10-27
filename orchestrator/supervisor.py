"""Health supervision and systemd integration (OPS-6, OPS-7, OPS-8).

The supervisor looks at every worker about every 5 seconds:

* a worker whose thread died is restarted (at most ``MAX_RESTARTS_PER_HOUR`` times);
* a worker that has not completed a cycle within 5 times its interval is **failed**;
* a worker whose outside world is in trouble (boiler Pi offline, Google unreachable) is
  **degraded**, which never causes a restart;
* the systemd watchdog is pinged **only while no critical component is failed**, so a hung
  Orchestrator is restarted by systemd, and an expected outside failure never is.

Everything the health endpoint says is answered from the last check (:meth:`Supervisor.latest`),
which is cheap.
"""

import http.client
import importlib
import logging
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from orchestrator.clock import Clock
from orchestrator.logging_setup import kv

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 5.0
FAILED_AFTER_INTERVALS = 5
MAX_RESTARTS_PER_HOUR = 5


class Health(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


class Notifier(Protocol):
    """What the service tells systemd (``Type=notify`` with a watchdog)."""

    def ready(self) -> None: ...

    def watchdog(self) -> None: ...

    def status(self, text: str) -> None: ...

    def stopping(self) -> None: ...


class NullNotifier:
    """Used when not started by systemd (development, tests)."""

    def ready(self) -> None:
        return None

    def watchdog(self) -> None:
        return None

    def status(self, text: str) -> None:
        return None

    def stopping(self) -> None:
        return None


class SystemdNotifier:
    """Talks to systemd through the ``NOTIFY_SOCKET`` (the ``sdnotify`` package)."""

    def __init__(self, backend: Any = None) -> None:
        self._notifier = backend or importlib.import_module("sdnotify").SystemdNotifier()

    def ready(self) -> None:
        self._notifier.notify("READY=1")

    def watchdog(self) -> None:
        self._notifier.notify("WATCHDOG=1")

    def status(self, text: str) -> None:
        self._notifier.notify("STATUS=" + text.replace("\n", " ")[:200])

    def stopping(self) -> None:
        self._notifier.notify("STOPPING=1")


def make_notifier() -> Notifier:
    """The systemd notifier when there is a notify socket, else one that does nothing."""
    if not os.environ.get("NOTIFY_SOCKET"):
        return NullNotifier()
    try:
        return SystemdNotifier()
    except (ImportError, OSError, AttributeError):
        logger.warning(kv("sd_notify_unavailable"))
        return NullNotifier()


@dataclass(slots=True)
class Component:
    """What the supervisor needs to know about one worker."""

    name: str
    interval_s: float  # how often it is expected to complete a cycle
    critical: bool
    last_cycle: Callable[[], float | None]  # monotonic time of its last finished cycle
    alive: Callable[[], bool] = lambda: True
    restart: Callable[[], None] | None = None
    degraded: Callable[[], str | None] = lambda: None  # a reason when the outside world is at fault
    info: Callable[[], tuple[datetime | None, str | None]] = lambda: (None, None)
    started_at: float = 0.0
    restarts: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    state: Health
    critical: bool
    detail: str
    seconds_since_cycle: float | None
    restarts_last_hour: int
    last_success: str | None
    last_error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "critical": self.critical,
            "detail": self.detail,
            "seconds_since_cycle": self.seconds_since_cycle,
            "restarts_last_hour": self.restarts_last_hour,
            "last_success": self.last_success,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: Health
    components: tuple[ComponentHealth, ...]
    version: str
    uptime_s: int
    facts: dict[str, Any]
    watchdog_ok: bool  # no critical component failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "version": self.version,
            "uptime_s": self.uptime_s,
            "components": {c.name: c.as_dict() for c in self.components},
            "system": self.facts,
        }


class Supervisor:
    def __init__(
        self,
        clock: Clock,
        components: Sequence[Component],
        *,
        version: str,
        notifier: Notifier | None = None,
        facts: Callable[[], dict[str, Any]] = lambda: {},
        ready: Callable[[], bool] = lambda: True,
        check_interval_s: float = CHECK_INTERVAL_S,
    ) -> None:
        self._clock = clock
        self._components = list(components)
        self._version = version
        self._notifier = notifier or NullNotifier()
        self._facts = facts
        self._ready = ready
        self._interval = check_interval_s
        self._lock = threading.Lock()
        self._started = clock.monotonic()
        for component in self._components:
            component.started_at = self._started
        self._announced_ready = False
        self._previous: dict[str, Health] = {}
        self._latest = HealthReport(Health.OK, (), version, 0, {}, True)
        self.last_cycle: float | None = None

    def latest(self) -> HealthReport:
        """The result of the last check (what ``/healthz`` answers)."""
        with self._lock:
            return self._latest

    # --- one check -------------------------------------------------------------------------

    def check_once(self) -> HealthReport:
        now = self._clock.monotonic()
        results = [self._check(component, now) for component in self._components]
        failed = [r for r in results if r.state is Health.FAILED]
        degraded = [r for r in results if r.state is Health.DEGRADED]
        status = Health.FAILED if failed else Health.DEGRADED if degraded else Health.OK
        report = HealthReport(
            status=status,
            components=tuple(results),
            version=self._version,
            uptime_s=int(now - self._started),
            facts=self._facts(),
            watchdog_ok=not any(r.critical for r in failed),
        )
        with self._lock:
            self._latest = report
        self._log_changes(results)
        self._tell_systemd(report)
        self.last_cycle = now
        return report

    def _check(self, component: Component, now: float) -> ComponentHealth:
        recent = [moment for moment in component.restarts if now - moment < 3600]
        component.restarts[:] = recent
        state, detail = Health.OK, "working"
        cycle = component.last_cycle()
        reference = max(cycle, component.started_at) if cycle is not None else component.started_at
        age = now - reference
        if not component.alive():
            state, detail = Health.FAILED, "the thread has stopped"
            self._restart(component, now)
        elif age > component.interval_s * FAILED_AFTER_INTERVALS:
            state = Health.FAILED
            detail = (
                f"no cycle finished for {age:.0f} s (expected every {component.interval_s:.0f} s)"
            )
        else:
            reason = component.degraded()
            if reason:
                state, detail = Health.DEGRADED, reason
        success, error = component.info()
        return ComponentHealth(
            name=component.name,
            state=state,
            critical=component.critical,
            detail=detail,
            seconds_since_cycle=None if cycle is None else round(now - cycle, 1),
            restarts_last_hour=len(component.restarts),
            last_success=success.isoformat(timespec="seconds") if success else None,
            last_error=error,
        )

    def _restart(self, component: Component, now: float) -> None:
        if component.restart is None:
            return
        if len(component.restarts) >= MAX_RESTARTS_PER_HOUR:
            logger.error(kv("worker_restart_limit", worker=component.name))
            return
        component.restarts.append(now)
        logger.error(kv("worker_restarting", worker=component.name))
        try:
            component.restart()
        except Exception:  # CS-11: a failing restart is retried at the next check, within the limit
            logger.exception(kv("worker_restart_failed", worker=component.name))
            return
        component.started_at = now  # a new thread gets a fresh start-up allowance

    def _log_changes(self, results: Sequence[ComponentHealth]) -> None:
        for result in results:
            before = self._previous.get(result.name, Health.OK)
            if result.state is not before:
                level = logging.INFO if result.state is Health.OK else logging.WARNING
                logger.log(
                    level,
                    kv(
                        "component_state",
                        component=result.name,
                        state=result.state.value,
                        detail=result.detail,
                    ),
                )
            self._previous[result.name] = result.state

    def _tell_systemd(self, report: HealthReport) -> None:
        if not self._announced_ready and self._ready():
            self._announced_ready = True
            self._notifier.ready()
            logger.info(kv("service_ready"))
        if report.watchdog_ok:
            self._notifier.watchdog()
        else:
            logger.error(kv("watchdog_ping_withheld"))
        summary = ", ".join(f"{c.name}={c.state.value}" for c in report.components)
        self._notifier.status(f"{report.status.value}: {summary}")

    # --- the thread ------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.check_once()
            except Exception:  # CS-11: the supervisor itself must keep going
                logger.exception(kv("supervisor_check_failed"))
            stop.wait(self._interval)
        self._notifier.stopping()

    def start(self, stop: threading.Event) -> threading.Thread:
        thread = threading.Thread(target=self.run, args=(stop,), name="supervisor", daemon=True)
        thread.start()
        return thread


class HttpProbe:
    """Asks the service's own web server for a page: a hung server is not 'alive'."""

    def __init__(self, clock: Clock, host: str, port: int, path: str = "/healthz") -> None:
        self._clock = clock
        self._host, self._port, self._path = host, port, path
        self._last_ok: float | None = None
        self.error: str | None = None

    def probe(self) -> bool:
        """Do one request; any answer at all counts (a 503 is still an answer)."""
        try:
            connection = http.client.HTTPConnection(self._host, self._port, timeout=3)
            try:
                connection.request("GET", self._path)
                connection.getresponse().read()
            finally:
                connection.close()
        except (OSError, http.client.HTTPException) as error:
            self.error = type(error).__name__
            return False
        self._last_ok = self._clock.monotonic()
        self.error = None
        return True

    def last_cycle(self) -> float | None:
        """Probe now and return the time of the last successful answer."""
        self.probe()
        return self._last_ok
