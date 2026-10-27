"""Supervisor, watchdog and the facts about the Pi (OPS-6, OPS-7, OPS-8)."""

import http.server
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orchestrator.supervisor import (
    FAILED_AFTER_INTERVALS,
    MAX_RESTARTS_PER_HOUR,
    Component,
    Health,
    HttpProbe,
    NullNotifier,
    Supervisor,
    SystemdNotifier,
    make_notifier,
)
from orchestrator.system_facts import (
    SystemFacts,
    clock_synchronized,
    cpu_temperature_c,
    disk_free_percent,
    memory_rss_mb,
)
from tests.fakes import FakeClock

NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))


class Recorder:
    """A notifier that remembers what systemd would have been told."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def ready(self) -> None:
        self.events.append("ready")

    def watchdog(self) -> None:
        self.events.append("watchdog")

    def status(self, text: str) -> None:
        self.events.append("status")
        self.last_status = text

    def stopping(self) -> None:
        self.events.append("stopping")

    last_status = ""


class Worker:
    """A fake worker: it can finish cycles, die, and be restarted."""

    def __init__(self, clock: FakeClock, interval_s: float = 10.0) -> None:
        self.clock = clock
        self.interval_s = interval_s
        self.cycle: float | None = None
        self.running = True
        self.restarts = 0
        self.outside_trouble: str | None = None

    def finish_cycle(self) -> None:
        self.cycle = self.clock.monotonic()

    def last_cycle(self) -> float | None:
        return self.cycle

    def alive(self) -> bool:
        return self.running

    def restart(self) -> None:
        self.restarts += 1
        self.running = True

    def component(
        self, name: str, *, critical: bool = False, restartable: bool = True
    ) -> Component:
        return Component(
            name,
            self.interval_s,
            critical,
            self.last_cycle,
            alive=self.alive,
            restart=self.restart if restartable else None,
            degraded=lambda: self.outside_trouble,
            info=lambda: (NOW, "last problem" if self.outside_trouble else None),
        )


class Rig:
    def __init__(self, *, ready: Callable[[], bool] = lambda: True) -> None:
        self.clock = FakeClock(NOW)
        self.notifier = Recorder()
        self.poller = Worker(self.clock)
        self.mail = Worker(self.clock, 30.0)
        self.supervisor = Supervisor(
            self.clock,
            [self.poller.component("poller", critical=True), self.mail.component("mail")],
            version="9.9.9",
            notifier=self.notifier,
            facts=lambda: {"memory_rss_mb": 42.0},
            ready=ready,
        )

    def all_cycle(self) -> None:
        self.poller.finish_cycle()
        self.mail.finish_cycle()

    def state(self, name: str) -> Health:
        return next(c.state for c in self.supervisor.latest().components if c.name == name)


@pytest.fixture
def rig() -> Rig:
    return Rig()


# --- states ---------------------------------------------------------------------------------


def test_working_workers_make_the_service_ok_and_the_watchdog_is_pinged(rig: Rig) -> None:
    rig.all_cycle()
    report = rig.supervisor.check_once()
    assert report.status is Health.OK and report.watchdog_ok
    assert rig.notifier.events.count("watchdog") == 1
    assert "poller=ok" in rig.notifier.last_status


def test_a_worker_gets_time_to_finish_its_first_cycle_after_the_start(rig: Rig) -> None:
    rig.clock.advance(timedelta(seconds=40))  # 4 intervals of the poller, no cycle yet
    assert rig.supervisor.check_once().status is Health.OK
    rig.clock.advance(timedelta(seconds=11))  # more than 5 intervals
    report = rig.supervisor.check_once()
    assert report.status is Health.FAILED and rig.state("poller") is Health.FAILED
    assert "no cycle finished" in report.components[0].detail


def test_a_worker_that_stops_finishing_cycles_is_failed_after_five_intervals(rig: Rig) -> None:
    rig.all_cycle()
    rig.clock.advance(timedelta(seconds=10 * FAILED_AFTER_INTERVALS - 1))
    assert rig.supervisor.check_once().status is not Health.FAILED
    rig.clock.advance(timedelta(seconds=2))
    assert rig.state("poller") is Health.OK  # not looked at yet
    rig.supervisor.check_once()
    assert rig.state("poller") is Health.FAILED


def test_trouble_in_the_outside_world_is_degraded_never_failed_and_never_restarted(
    rig: Rig,
) -> None:
    rig.all_cycle()
    rig.poller.outside_trouble = "the boiler Pi does not answer"
    report = rig.supervisor.check_once()
    assert report.status is Health.DEGRADED and report.watchdog_ok
    assert report.components[0].detail == "the boiler Pi does not answer"
    assert rig.poller.restarts == 0
    assert "watchdog" in rig.notifier.events  # a degraded service is not restarted by systemd


# --- the watchdog -------------------------------------------------------------------------


def test_a_failed_critical_worker_stops_the_watchdog_pings(rig: Rig) -> None:
    rig.all_cycle()
    rig.supervisor.check_once()
    rig.clock.advance(timedelta(seconds=60))
    rig.mail.finish_cycle()
    report = rig.supervisor.check_once()
    assert rig.state("poller") is Health.FAILED and not report.watchdog_ok
    assert rig.notifier.events.count("watchdog") == 1  # only the first, healthy check pinged


def test_a_failed_worker_that_is_not_critical_keeps_the_watchdog_going(rig: Rig) -> None:
    rig.all_cycle()
    rig.supervisor.check_once()
    rig.clock.advance(timedelta(seconds=200))  # the mail check is silent, the poller cycles
    rig.poller.finish_cycle()
    report = rig.supervisor.check_once()
    assert rig.state("mail") is Health.FAILED and report.status is Health.FAILED
    assert report.watchdog_ok
    assert rig.notifier.events.count("watchdog") == 2


def test_a_checker_that_stops_calling_means_no_pings_at_all() -> None:
    made = Rig()
    made.all_cycle()
    made.supervisor.check_once()
    pings = made.notifier.events.count("watchdog")
    made.clock.advance(timedelta(seconds=120))
    assert made.notifier.events.count("watchdog") == pings  # nothing pings on its own


# --- ready ------------------------------------------------------------------------------------


def test_ready_is_sent_once_and_only_when_the_web_server_listens() -> None:
    listening = [False]
    made = Rig(ready=lambda: listening[0])
    made.all_cycle()
    made.supervisor.check_once()
    assert "ready" not in made.notifier.events
    listening[0] = True
    made.supervisor.check_once()
    made.supervisor.check_once()
    assert made.notifier.events.count("ready") == 1


# --- dead threads --------------------------------------------------------------------------


def test_a_dead_worker_is_restarted_and_is_ok_again_when_it_cycles(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.all_cycle()
    rig.mail.running = False
    with caplog.at_level(logging.ERROR):
        report = rig.supervisor.check_once()
    assert rig.mail.restarts == 1 and rig.state("mail") is Health.FAILED
    assert report.components[1].restarts_last_hour == 1
    assert "event=worker_restarting" in caplog.text
    rig.clock.advance(timedelta(seconds=5))
    rig.all_cycle()
    assert rig.supervisor.check_once().status is Health.OK


def test_restarts_are_limited_to_five_per_hour(rig: Rig) -> None:
    rig.all_cycle()
    for _ in range(MAX_RESTARTS_PER_HOUR + 3):
        rig.mail.running = False
        rig.clock.advance(timedelta(seconds=10))
        rig.poller.finish_cycle()
        rig.supervisor.check_once()
    assert rig.mail.restarts == MAX_RESTARTS_PER_HOUR
    assert rig.state("mail") is Health.FAILED
    rig.clock.advance(timedelta(hours=1, seconds=1))
    rig.poller.finish_cycle()
    rig.supervisor.check_once()
    assert rig.mail.restarts == MAX_RESTARTS_PER_HOUR + 1  # the hour is over: it may try again


class BrokenRestart(Worker):
    def restart(self) -> None:
        raise RuntimeError("cannot start")


def test_a_restart_that_fails_is_logged_and_does_not_stop_the_supervisor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(NOW)
    worker = BrokenRestart(clock)
    worker.finish_cycle()
    worker.running = False
    supervisor = Supervisor(clock, [worker.component("w")], version="v")
    with caplog.at_level(logging.ERROR):
        assert supervisor.check_once().status is Health.FAILED
    assert "event=worker_restart_failed" in caplog.text


def test_a_dead_worker_without_a_restart_function_stays_failed(rig: Rig) -> None:
    other = Worker(rig.clock)
    sup = Supervisor(rig.clock, [other.component("x", restartable=False)], version="v")
    other.finish_cycle()
    other.running = False
    assert sup.check_once().status is Health.FAILED and other.restarts == 0


def test_state_changes_are_logged_once(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.all_cycle()
    with caplog.at_level(logging.INFO):
        rig.supervisor.check_once()
        rig.poller.outside_trouble = "offline"
        rig.supervisor.check_once()
        rig.supervisor.check_once()
        rig.poller.outside_trouble = None
        rig.supervisor.check_once()
    lines = [r.message for r in caplog.records if "component_state" in r.message]
    assert [("state=degraded" in m, "state=ok" in m) for m in lines] == [
        (True, False),
        (False, True),
    ]


# --- what the endpoint says -----------------------------------------------------------------


def test_the_report_has_every_component_with_times_errors_and_the_system_facts(rig: Rig) -> None:
    rig.all_cycle()
    rig.poller.outside_trouble = "offline"
    data = rig.supervisor.check_once().as_dict()
    assert data["status"] == "degraded" and data["version"] == "9.9.9"
    assert data["system"] == {"memory_rss_mb": 42.0}
    poller = data["components"]["poller"]
    assert poller["state"] == "degraded" and poller["critical"] is True
    assert poller["last_success"] == "2026-10-24T16:41:00+03:00"
    assert poller["last_error"] == "last problem"
    assert data["components"]["mail"]["state"] == "ok"


def test_latest_is_the_last_check_and_costs_nothing(rig: Rig) -> None:
    assert rig.supervisor.latest().status is Health.OK
    rig.all_cycle()
    rig.supervisor.check_once()
    calls = rig.notifier.events.count("status")
    for _ in range(5):
        rig.supervisor.latest()
    assert rig.notifier.events.count("status") == calls


def test_uptime_counts_from_the_start(rig: Rig) -> None:
    rig.clock.advance(timedelta(seconds=125))
    rig.all_cycle()
    assert rig.supervisor.check_once().uptime_s == 125


# --- the thread -------------------------------------------------------------------------------


def wait_until(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_the_thread_checks_repeatedly_survives_errors_and_says_stopping() -> None:
    clock = FakeClock(NOW)
    notifier = Recorder()
    worker = Worker(clock)
    worker.finish_cycle()
    calls = {"facts": 0}

    def facts() -> dict[str, float]:
        calls["facts"] += 1
        if calls["facts"] == 2:
            raise RuntimeError("bug in a facts reader")
        return {}

    supervisor = Supervisor(
        clock,
        [worker.component("w")],
        version="v",
        notifier=notifier,
        facts=facts,
        check_interval_s=0.02,
    )
    stop = threading.Event()
    thread = supervisor.start(stop)
    assert wait_until(lambda: calls["facts"] >= 4)
    assert supervisor.last_cycle is not None
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive() and notifier.events[-1] == "stopping"


# --- systemd ---------------------------------------------------------------------------------


def test_without_a_notify_socket_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert isinstance(make_notifier(), NullNotifier)
    quiet = NullNotifier()
    quiet.ready()
    quiet.watchdog()
    quiet.status("x")
    quiet.stopping()


def test_the_systemd_messages_have_the_documented_form(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    class Socket:
        def notify(self, text: str) -> None:
            sent.append(text)

    notifier = SystemdNotifier(Socket())
    notifier.ready()
    notifier.watchdog()
    notifier.status("ok: a=ok\nb=ok")
    notifier.stopping()
    assert sent == ["READY=1", "WATCHDOG=1", "STATUS=ok: a=ok b=ok", "STOPPING=1"]


# --- probing our own web server ---------------------------------------------------------------


class _Quiet(http.server.BaseHTTPRequestHandler):
    code = 200

    def do_GET(self) -> None:
        self.send_response(self.code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        return None


def serve(code: int) -> tuple[http.server.HTTPServer, threading.Thread]:
    handler = type("Handler", (_Quiet,), {"code": code})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.mark.parametrize("code", [200, 503])
def test_any_answer_from_the_web_server_counts_as_alive(code: int) -> None:
    server, thread = serve(code)
    try:
        clock = FakeClock(NOW)
        probe = HttpProbe(clock, "127.0.0.1", server.server_port)
        assert probe.last_cycle() == clock.monotonic() and probe.error is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_no_answer_keeps_the_old_time_and_names_the_kind_of_error() -> None:
    server, thread = serve(200)
    port = server.server_port
    clock = FakeClock(NOW)
    probe = HttpProbe(clock, "127.0.0.1", port)
    first = probe.last_cycle()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    clock.advance(timedelta(seconds=30))
    assert probe.last_cycle() == first and probe.error is not None


# --- facts about the Pi ---------------------------------------------------------------------


def test_readers_return_nothing_rather_than_a_guess(tmp_path: Path) -> None:
    assert memory_rss_mb(tmp_path / "missing") is None
    assert cpu_temperature_c(tmp_path / "missing") is None
    assert disk_free_percent(tmp_path / "missing" / "deeper") is None
    bad = tmp_path / "bad"
    bad.write_text("not numbers", encoding="ascii")
    assert memory_rss_mb(bad) is None and cpu_temperature_c(bad) is None


def test_readers_understand_the_linux_files(tmp_path: Path) -> None:
    (tmp_path / "temp").write_text("52384\n", encoding="ascii")
    assert cpu_temperature_c(tmp_path / "temp") == 52.4
    (tmp_path / "statm").write_text("1000 5120 300 10 0 800 0\n", encoding="ascii")
    value = memory_rss_mb(tmp_path / "statm")
    assert value is not None and value > 0
    free = disk_free_percent(tmp_path)
    assert free is not None and 0 <= free <= 100


def test_the_clock_check_is_a_boolean_or_unknown() -> None:
    assert clock_synchronized() in (True, False, None)


def test_facts_are_read_at_most_every_half_minute_and_a_failing_reader_is_contained(
    tmp_path: Path,
) -> None:
    now = [0.0]
    reads = {"temp": 0}

    def temperature() -> float | None:
        reads["temp"] += 1
        return 51.0

    def broken() -> bool | None:
        raise RuntimeError("bug")

    facts = SystemFacts(
        tmp_path,
        monotonic=lambda: now[0],
        memory=lambda: 40.0,
        temperature=temperature,
        clock_sync=broken,
    )
    first = facts.read()
    assert first["memory_rss_mb"] == 40.0 and first["cpu_temp_c"] == 51.0
    assert first["clock_synchronized"] is None
    assert 0 <= (first["disk_free_percent"] or 0) <= 100
    now[0] = 29.0
    facts.read()
    assert reads["temp"] == 1
    now[0] = 31.0
    facts.read()
    assert reads["temp"] == 2
