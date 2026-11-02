"""The running service: builds every part, starts it in the order of OPS-2, and stops it cleanly.

``python -m orchestrator`` calls :func:`main`. :class:`Application` takes the outside connections
(boiler HTTP session, Google services, MQTT link, SSH connector, systemd notifier) as optional
overrides, so that the whole program can be started in tests against fakes, with no real host.

Start-up (OPS-2): the clock is waited for (the Pi has no battery clock; until it is synchronized
nothing is pushed or sent to the boiler Pi), the last good schedule files are loaded by the push
queue, the workers start, the web server starts, and READY is sent to systemd once the web server
answers. Nothing waits for the boiler Pi, Google or the MQTT broker: a missing one only makes the
service *degraded*, and a missing Google authorization shows as "Google authorization needed".
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

import requests

from orchestrator import __version__
from orchestrator.admin_notifier import AdminNotifier
from orchestrator.astro import load_location
from orchestrator.boiler_client import BoilerClient
from orchestrator.calendar_source import (
    CalendarAuthError,
    CalendarRead,
    GoogleCalendarSource,
    build_calendar_service,
)
from orchestrator.calendar_sync import CalendarSync
from orchestrator.calendar_writer import GoogleCalendarWriter
from orchestrator.clock import Clock, system_clock
from orchestrator.config import Config, ConfigError, load_config
from orchestrator.diagnostics import Diagnostics
from orchestrator.google_auth import (
    CALENDAR_READONLY_SCOPE,
    CALENDAR_SCOPE,
    GMAIL_SCOPE,
    AuthError,
    granted_scopes,
    load_credentials,
    status_for,
)
from orchestrator.logging_setup import (
    DebugWindow,
    install_debug_signal,
    log_startup_banner,
    setup_logging,
)
from orchestrator.mail_client import (
    GoogleMailClient,
    InboxRead,
    MailAuthError,
    MailClient,
    OutgoingMail,
    build_mail_service,
)
from orchestrator.mail_commands import MailCommands
from orchestrator.mail_gate import SecurityAlerts
from orchestrator.mail_worker import MailWorker
from orchestrator.manual_commands import ManualCommands
from orchestrator.mqtt_bridge import MqttBridge, MqttLink, build_link
from orchestrator.push_queue import PushQueue
from orchestrator.reboot_grace import RebootGrace
from orchestrator.relays import RelayNames
from orchestrator.schedule_editor import ScheduleEditor
from orchestrator.ssh_reboot import Connector, SshReboot, SshTarget, connect_with_paramiko
from orchestrator.status_cache import StatusCache, StatusPoller
from orchestrator.supervisor import (
    Component,
    Health,
    HealthReport,
    HttpProbe,
    Notifier,
    Supervisor,
    make_notifier,
)
from orchestrator.system_facts import SystemFacts, clock_synchronized
from orchestrator.web_api import HistoryProvider, WebDeps, WebServer, create_app, make_server

logger = logging.getLogger(__name__)

CLOCK_POLL_S = 2.0
STOP_MARKER = "last_stop.json"
STARTED_MAIL_TRIES = 10
STARTED_MAIL_RETRY_S = 30.0


@dataclass(frozen=True, slots=True)
class GoogleAccess:
    """What Google access there is: a service for the calendar and one for the mail."""

    calendar: Any
    mail: Any
    calendar_note: str  # why there is no calendar access (empty when there is)
    mail_note: str
    calendar_writable: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Overrides:
    """Replace outside connections (tests, development); ``None`` means the real one."""

    clock: Clock | None = None
    boiler_session: requests.Session | None = None
    calendar_service: Any = None
    gmail_service: Any = None
    mqtt_link: MqttLink | None = None
    ssh_connect: Connector | None = None
    notifier: Notifier | None = None
    web_port: int | None = None
    clock_check: Callable[[], bool | None] | None = None
    clock_wait_s: float = 120.0
    diagnostics_interval_s: float = 60.0
    facts: Callable[[], dict[str, Any]] | None = None  # the Pi's disk, memory, temperature, clock


# --- stand-ins used while Google access is missing -----------------------------------------------


class _NoCalendarAccess:
    """Reading says 'authorization needed', so the UI, MQTT and the admin mail all show it."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def read(self, window_start: datetime, window_end: datetime) -> CalendarRead:
        raise CalendarAuthError(self._reason)


class _NoMailAccess:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def own_address(self) -> str:
        raise MailAuthError(self._reason)

    def list_inbox(self, limit: int = 20) -> InboxRead:
        raise MailAuthError(self._reason)

    def remove(self, message_id: str) -> None:
        raise MailAuthError(self._reason)

    def send(self, mail: OutgoingMail) -> None:
        raise MailAuthError(self._reason)


class _ClockGate:
    """True once the clock is synchronized (or cannot be asked); asked again every few seconds."""

    def __init__(self, check: Callable[[], bool | None], monotonic: Callable[[], float]) -> None:
        self._check = check
        self._monotonic = monotonic
        self._ok = False
        self._asked_at: float | None = None
        self._lock = threading.Lock()

    def ok(self) -> bool:
        with self._lock:
            now = self._monotonic()
            if not self._ok and (self._asked_at is None or now - self._asked_at >= CLOCK_POLL_S):
                self._asked_at = now
                self._ok = self._check() is not False  # unknown (None) is not a reason to wait
            return self._ok


class _Worker:
    """A thread that the supervisor may restart."""

    def __init__(self, name: str, start: Callable[[threading.Event], threading.Thread]) -> None:
        self.name = name
        self._start = start
        self.thread: threading.Thread | None = None

    def launch(self, stop: threading.Event) -> None:
        self.thread = self._start(stop)

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class Application:
    def __init__(
        self,
        config: Config,
        *,
        overrides: Overrides | None = None,
        log_lines: Callable[[], list[str]] = lambda: [],
    ) -> None:
        self.config = config
        self._o = overrides or Overrides()
        self._log_lines = log_lines
        self.clock: Clock = self._o.clock or system_clock(config)
        self.stop_event = threading.Event()
        self._workers: dict[str, _Worker] = {}
        self._server: WebServer | None = None
        self._server_thread: threading.Thread | None = None
        self.web_port = 0
        self.self_check: list[dict[str, Any]] = []
        self.supervisor: Supervisor | None = None
        self._threads_to_join: list[threading.Thread] = []

    # --- building and starting ----------------------------------------------------------------

    def start(self) -> None:
        config = self.config
        data_dir = Path(config.general.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        previous_stop = self._read_stop_marker(data_dir)
        self._read_facts: Callable[[], dict[str, Any]] = self._o.facts or SystemFacts(data_dir).read

        self.cache = StatusCache()
        self.boiler = BoilerClient(
            config.boiler, session=self._o.boiler_session, dry_run=config.general.dry_run
        )
        gate = _ClockGate(self._o.clock_check or clock_synchronized, time.monotonic)
        self._wait_for_clock(gate)
        self.reboot_grace = RebootGrace(self.clock, timedelta(seconds=config.boiler.reboot_grace_s))

        access = self._google_access()
        mail_client: MailClient = (
            GoogleMailClient(access.mail) if access.mail else _NoMailAccess(access.mail_note)
        )
        self.notifier = AdminNotifier(
            mail_client, config.mail.admin_email, self.clock, reboot_grace=self.reboot_grace
        )
        self.poller = StatusPoller(
            self.boiler,
            self.cache,
            self.clock,
            config.boiler,
            on_transition=self.notifier.on_transition,
        )
        self.manual = ManualCommands(self.boiler, self.cache, refresh=self.poller.poll_once)
        self.push = PushQueue(self.boiler, data_dir, self.clock, gate=gate.ok)
        names = RelayNames(config.relays.names)
        source = (
            GoogleCalendarSource(access.calendar, config.calendar.calendar_id, self.clock.tz)
            if access.calendar
            else _NoCalendarAccess(access.calendar_note)
        )
        self.sync = CalendarSync(
            source,
            self.push,
            self.clock,
            location=lambda: load_location(config),
            relay_names=names,
            max_showers=_decimal(config.limits.max_showers),
            interval_s=config.calendar.poll_interval_s,
            on_report=self.notifier.on_sync_report,
        )
        editor = None
        if access.calendar:
            editor = ScheduleEditor(
                GoogleCalendarWriter(access.calendar, config.calendar.calendar_id, self.clock.tz),
                self.clock,
                enabled=config.calendar.write_back and access.calendar_writable,
                relay_names=names,
                max_showers=_decimal(config.limits.max_showers),
                on_changed=self.sync.request_sync,
            )
        self.mail_worker = self._build_mail(mail_client, names)
        self.diagnostics = Diagnostics(
            self.clock,
            self.notifier,
            facts=self._read_facts,
            push_statuses=self.push.statuses,
            health=lambda: (
                self.supervisor.latest()
                if self.supervisor
                else HealthReport(Health.OK, (), __version__, 0, {}, True)
            ),
            sync_report=self.sync.latest,
            version=__version__,
            interval_s=self._o.diagnostics_interval_s,
        )
        self.mqtt = self._build_mqtt(names)

        self._add("status-poller", self.poller.start)
        self._add("push-queue", self.push.start)
        self._add("calendar", self.sync.start)
        self._add("mail", self.mail_worker.start)
        self._add("diagnostics", self.diagnostics.start)
        if self.mqtt is not None:
            self._add("mqtt", self.mqtt.start)
        for worker in self._workers.values():
            worker.launch(self.stop_event)

        deps = WebDeps(
            config=config,
            clock=self.clock,
            cache=self.cache,
            manual=self.manual,
            history=HistoryProvider(self.boiler.get_history, self.clock.monotonic),
            sync=self.sync,
            editor=editor,
            push_statuses=self.push.statuses,
            log_lines=self._log_lines,
            relay_names=names,
            health=lambda: self.supervisor.latest().as_dict() if self.supervisor else {},
            push_now=self.push.push_now,
        )
        port = self._o.web_port if self._o.web_port is not None else config.web.port
        self._server = make_server(create_app(deps), config.web.host, port)
        self.web_port = self._server.effective_port
        self._server_thread = threading.Thread(target=self._server.run, name="web", daemon=True)
        self._server_thread.start()

        self.self_check = self._run_self_check(access, gate)
        probe = HttpProbe(self.clock, "127.0.0.1", self.web_port)
        self.supervisor = Supervisor(
            self.clock,
            self._components(probe),
            version=__version__,
            notifier=self._o.notifier or make_notifier(),
            facts=lambda: {**self._read_facts(), "self_check": self.self_check},
            ready=probe.probe,
        )
        self.supervisor.check_once()  # READY goes out as soon as the web server answers
        self._threads_to_join.append(self.supervisor.start(self.stop_event))
        self._start_announcement(mail_client, previous_stop)
        logger.info(kv_started(self.web_port))

    def _add(self, name: str, start: Callable[[threading.Event], threading.Thread]) -> None:
        self._workers[name] = _Worker(name, start)

    def _google_access(self) -> GoogleAccess:
        """The Calendar and Gmail services, each with its own token, or why one is missing.

        The mail needs a token with the full mail permission (``GOOGLE_TOKEN_FILE``). The calendar
        uses ``GOOGLE_CALENDAR_TOKEN_FILE`` when that file exists, else the same token; a read-only
        calendar permission is enough to read, and only the full one allows page edits.
        """
        o = self._o
        if o.calendar_service is not None or o.gmail_service is not None:
            return GoogleAccess(
                o.calendar_service,
                o.gmail_service,
                "" if o.calendar_service is not None else "no calendar service was given",
                "" if o.gmail_service is not None else "no mail service was given",
                calendar_writable=o.calendar_service is not None,
                detail="injected",
            )
        secrets = self.config.secrets
        main = Path(secrets.google_token_file) if secrets.google_token_file else None
        own = (
            Path(secrets.google_calendar_token_file) if secrets.google_calendar_token_file else None
        )
        calendar_path = own if own is not None and own.exists() else main
        mail, mail_note = self._google_service(main, "mail", build_mail_service)
        calendar, calendar_note = self._google_service(
            calendar_path, "calendar", build_calendar_service
        )
        writable = (
            calendar is not None
            and calendar_path is not None
            and CALENDAR_SCOPE in granted_scopes(calendar_path)
        )
        mail_text = mail_note or "mail ok"
        calendar_text = calendar_note or (
            "calendar read-write" if writable else "calendar read-only"
        )
        return GoogleAccess(
            calendar, mail, calendar_note, mail_note, writable, f"{mail_text}; {calendar_text}"
        )

    @staticmethod
    def _google_service(
        path: Path | None, purpose: str, build: Callable[[Any], Any]
    ) -> tuple[Any, str]:
        if path is None:
            return None, "GOOGLE_TOKEN_FILE is not set; run python -m orchestrator.auth"
        status = status_for(path, purpose)
        if not status.usable:
            return None, f"Google authorization needed ({purpose}: {status.state.value})"
        try:
            if purpose == "mail":
                credentials = load_credentials(path, scopes=(GMAIL_SCOPE,))
            else:
                credentials = load_credentials(
                    path, scopes=(), any_of=(CALENDAR_SCOPE, CALENDAR_READONLY_SCOPE)
                )
            return build(credentials), ""
        except (AuthError, ImportError, OSError) as error:
            logger.error(
                "event=google_service_failed purpose=%s error=%s", purpose, type(error).__name__
            )
            return None, f"Google {purpose} service could not be created"

    def _build_mail(self, client: MailClient, names: RelayNames) -> MailWorker:
        config = self.config
        alerts = SecurityAlerts(
            client,
            config.mail.security_alert_email,
            self.clock,
            daily_max=config.mail.alert_daily_max,
        )
        boiler = config.boiler
        secrets = config.secrets
        rebooter = SshReboot(
            SshTarget(
                host=urlsplit(boiler.base_url).hostname or "",
                port=boiler.ssh_port,
                user=secrets.ssh_user,
                key_file=secrets.ssh_key_file,
                password=secrets.ssh_password,
                host_key=boiler.ssh_host_key,
                timeout_s=boiler.ssh_timeout_s,
            ),
            connect=self._o.ssh_connect or connect_with_paramiko,
            on_requested=self.reboot_grace.begin,
        )
        commands = MailCommands(
            client,
            self.manual,
            self.cache,
            self.clock,
            alerts,
            log_text=self.boiler.get_log,
            rebooter=rebooter,
            admin_email=config.mail.admin_email,
            stale_after=timedelta(seconds=boiler.stale_after_s),
            dry_run=config.general.dry_run,
        )
        return MailWorker(
            client,
            commands,
            alerts,
            config.mail.user_email_list,
            interval_s=config.mail.check_interval_s,
            on_auth_change=self.notifier.on_mail_auth,
        )

    def _build_mqtt(self, names: RelayNames) -> MqttBridge | None:
        config = self.config
        if not config.mqtt.enabled:
            return None
        link = self._o.mqtt_link or build_link(config.mqtt, config.secrets.mqtt_password)
        return MqttBridge(
            link,
            config.mqtt,
            manual=self.manual,
            cache=self.cache,
            clock=self.clock,
            relay_names=names,
            version=__version__,
            stale_after=timedelta(seconds=config.boiler.stale_after_s),
            diagnostics=self.diagnostics.mqtt_values,
        )

    def _components(self, probe: HttpProbe) -> list[Component]:
        config = self.config
        workers = self._workers

        def component(
            name: str,
            interval: float,
            critical: bool,
            cycle: Callable[[], float | None],
            degraded: Callable[[], str | None],
            info: Callable[[], tuple[datetime | None, str | None]],
        ) -> Component:
            worker = workers[name]
            return Component(
                name,
                interval,
                critical,
                cycle,
                worker.alive,
                lambda: worker.launch(self.stop_event),
                degraded,
                info,
            )

        def boiler_info() -> tuple[datetime | None, str | None]:
            snapshot = self.cache.snapshot()
            return snapshot.fetched_at, snapshot.error

        def boiler_degraded() -> str | None:
            snapshot = self.cache.snapshot()
            if snapshot.state.unhealthy:
                return f"boiler Pi {snapshot.state.value}: {snapshot.error or 'no details'}"
            return None

        def push_degraded() -> str | None:
            failing = [s for s in self.push.statuses() if s.last_error]
            return f"pushing failing: {failing[0].last_error}" if failing else None

        def push_info() -> tuple[datetime | None, str | None]:
            statuses = self.push.statuses()
            pushed = [s.last_pushed_at for s in statuses if s.last_pushed_at]
            errors = [s.last_error for s in statuses if s.last_error]
            return (max(pushed) if pushed else None), (errors[0] if errors else None)

        def calendar_degraded() -> str | None:
            report = self.sync.latest()
            if report is None or report.ok:
                return None
            return (
                "Google authorization needed" if report.auth_lost else f"calendar: {report.error}"
            )

        def calendar_info() -> tuple[datetime | None, str | None]:
            report = self.sync.latest()
            if report is None:
                return None, None
            return (report.at if report.ok else None), report.error

        def mail_degraded() -> str | None:
            return "Google authorization needed" if self.mail_worker.auth_lost else None

        poll = config.boiler.poll_interval_s
        parts = [
            component(
                "status-poller",
                poll,
                True,
                lambda: self.poller.last_cycle,
                boiler_degraded,
                boiler_info,
            ),
            component(
                "push-queue", 5.0, False, lambda: self.push.last_cycle, push_degraded, push_info
            ),
            component(
                "calendar",
                config.calendar.poll_interval_s,
                False,
                lambda: self.sync.last_cycle,
                calendar_degraded,
                calendar_info,
            ),
            component(
                "mail",
                config.mail.check_interval_s,
                False,
                lambda: self.mail_worker.last_cycle,
                mail_degraded,
                lambda: (None, None),
            ),
            component(
                "diagnostics",
                self._o.diagnostics_interval_s,
                False,
                lambda: self.diagnostics.last_cycle,
                lambda: None,
                lambda: (None, None),
            ),
        ]
        if self.mqtt is not None:
            bridge = self.mqtt
            parts.append(
                component(
                    "mqtt",
                    10.0,
                    False,
                    lambda: bridge.last_cycle,
                    lambda: None if bridge.connected else "not connected to the broker",
                    lambda: (None, None),
                )
            )
        server_thread = self._server_thread
        parts.append(
            Component(
                "web",
                10.0,
                True,
                probe.last_cycle,
                alive=lambda: server_thread is not None and server_thread.is_alive(),
                info=lambda: (None, probe.error),
            )
        )
        return parts

    # --- the clock, the self-check, the start mail ---------------------------------------------

    def _wait_for_clock(self, gate: _ClockGate) -> None:
        deadline = time.monotonic() + self._o.clock_wait_s
        while not gate.ok() and time.monotonic() < deadline and not self.stop_event.is_set():
            self.stop_event.wait(CLOCK_POLL_S)
        if not gate.ok():
            logger.warning(
                "event=clock_not_synchronized action=%s",
                "no schedule or manual files are sent until it is",
            )

    def _run_self_check(self, access: GoogleAccess, gate: _ClockGate) -> list[dict[str, Any]]:
        config = self.config
        facts = self._read_facts()
        secrets = {
            "BOILER_SSH_USER": config.secrets.ssh_user,
            "BOILER_SSH_PASSWORD or BOILER_SSH_KEY_FILE": (
                config.secrets.ssh_password or config.secrets.ssh_key_file
            ),
            "MQTT_PASSWORD": config.secrets.mqtt_password,
            "UI_PASSWORD": config.secrets.ui_password,
            "GOOGLE_TOKEN_FILE": config.secrets.google_token_file,
            "GOOGLE_CALENDAR_TOKEN_FILE": config.secrets.google_calendar_token_file,
        }
        free = facts.get("disk_free_percent")
        checks = [
            {"check": "configuration", "ok": True, "detail": "loaded and valid"},
            {
                "check": "secrets",
                "ok": True,
                "detail": "set: " + (", ".join(k for k, v in secrets.items() if v) or "none"),
            },
            {
                "check": "clock",
                "ok": gate.ok(),
                "detail": "synchronized" if gate.ok() else "waiting",
            },
            {
                "check": "disk",
                "ok": free is None or free >= 10,
                "detail": "unknown" if free is None else f"{free}% free",
            },
            {
                "check": "google",
                "ok": not (access.mail_note or access.calendar_note),
                "detail": access.detail,
            },
            {"check": "dry run", "ok": True, "detail": "on" if config.general.dry_run else "off"},
        ]
        for item in checks:
            logger.info(
                "event=self_check check=%s ok=%s detail=%s",
                item["check"],
                item["ok"],
                item["detail"],
            )
        return checks

    def _read_stop_marker(self, data_dir: Path) -> str:
        path = data_dir / STOP_MARKER
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
            path.unlink()
            return f"clean stop at {marker.get('at', 'an unknown time')}"
        except FileNotFoundError:
            return "unknown (no clean stop was recorded: power loss, crash or first start)"
        except (OSError, ValueError):
            return "unknown"

    def _start_announcement(self, client: MailClient, previous_stop: str) -> None:
        lines = [
            f"{c['check']}: {'ok' if c['ok'] else 'PROBLEM'} - {c['detail']}"
            for c in self.self_check
        ]

        def send() -> None:
            for _ in range(STARTED_MAIL_TRIES):
                if self.notifier.started(__version__, previous_stop, lines):
                    return
                if self.stop_event.wait(STARTED_MAIL_RETRY_S):
                    return

        if not self.config.mail.admin_email:
            return
        thread = threading.Thread(target=send, name="start-mail", daemon=True)
        thread.start()
        self._threads_to_join.append(thread)

    # --- stopping ---------------------------------------------------------------------------

    def stop(self) -> None:
        """Stop everything, in the reverse order; safe to call more than once."""
        self.stop_event.set()
        self.push.wake()
        self.sync.request_sync()
        if self._server is not None:
            self._server.close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=5)
        for worker in self._workers.values():
            if worker.thread is not None:
                worker.thread.join(timeout=5)
        for thread in self._threads_to_join:
            thread.join(timeout=5)
        self._write_stop_marker()
        logger.info("event=stopped")

    def _write_stop_marker(self) -> None:
        path = Path(self.config.general.data_dir) / STOP_MARKER
        try:
            path.write_text(
                json.dumps({"at": self.clock.now().isoformat(timespec="seconds")}), encoding="utf-8"
            )
        except OSError:
            logger.warning("event=stop_marker_not_written")

    def wait(self) -> None:
        """Block until a stop is requested (SIGTERM or Ctrl-C in :func:`main`)."""
        self.stop_event.wait()


def kv_started(port: int) -> str:
    return f"event=started version={__version__} web_port={port}"


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


# --- the command line ---------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stream: IO[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator", description="Home Control")
    parser.add_argument("--config", help="configuration file (default: $ORCHESTRATOR_CONFIG)")
    args = parser.parse_args(argv)
    environment = os.environ if env is None else env
    out = stream or sys.stderr
    path = args.config or environment.get("ORCHESTRATOR_CONFIG")
    try:
        config = load_config(Path(path) if path else None, env=environment)
    except ConfigError as error:
        print(f"configuration error: {error}", file=out)
        return 2
    handles = setup_logging(config)
    log_startup_banner(logger, config)
    install_debug_signal(DebugWindow(config.logging.level))
    application = Application(config, log_lines=handles.ring.snapshot)
    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, lambda _n, _f: application.stop_event.set())
    try:
        application.start()
        application.wait()
    except Exception:
        logger.exception("event=fatal")
        return 1
    finally:
        if hasattr(application, "push"):
            application.stop()
        handles.close()
    return 0
