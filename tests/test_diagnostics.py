"""Resource alerts, the hourly heartbeat line and the values for Home Assistant (OPS-9..11)."""

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orchestrator.admin_notifier import AdminNotifier
from orchestrator.calendar_sync import SyncReport
from orchestrator.diagnostics import Diagnostics, Limits
from orchestrator.mail_client import GoogleMailClient
from orchestrator.push_queue import PushStatus, ScheduleKind
from orchestrator.supervisor import ComponentHealth, Health, HealthReport
from tests.fake_gmail import FakeGmailService
from tests.fake_google import FakeHttpError
from tests.fakes import FakeClock

NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
ADMIN = "admin@example.com"
MINUTE = timedelta(minutes=1)


def component(name: str, state: Health = Health.OK, age: float | None = 3.0) -> ComponentHealth:
    return ComponentHealth(name, state, False, "", age, 0, None, None)


def push_status(kind: ScheduleKind, *, pending: bool, error: str | None = None) -> PushStatus:
    return PushStatus(kind, True, pending, None, None, error, 0, None)


class Setup:
    def __init__(self, *, daily_max: int = 30, limits: Limits | None = None) -> None:
        self.clock = FakeClock(NOW)
        self.gmail = FakeGmailService("orchestrator@example.com")
        self.facts: dict[str, Any] = {
            "memory_rss_mb": 60.0,
            "cpu_temp_c": 50.0,
            "disk_free_percent": 55.0,
            "clock_synchronized": True,
        }
        self.pushes: list[PushStatus] = []
        self.components: list[ComponentHealth] = [component("status-poller"), component("calendar")]
        self.sync: SyncReport | None = None
        self.notifier = AdminNotifier(
            GoogleMailClient(self.gmail), ADMIN, self.clock, daily_max=daily_max
        )
        self.diagnostics = Diagnostics(
            self.clock,
            self.notifier,
            facts=lambda: dict(self.facts),
            push_statuses=lambda: list(self.pushes),
            health=self.report,
            sync_report=lambda: self.sync,
            version="9.9.9",
            limits=limits,
            interval_s=0.02,
        )

    def report(self) -> HealthReport:
        states = [c.state for c in self.components]
        status = (
            Health.FAILED
            if Health.FAILED in states
            else (Health.DEGRADED if Health.DEGRADED in states else Health.OK)
        )
        return HealthReport(status, tuple(self.components), "9.9.9", 7380, {}, True)

    def subjects(self) -> list[str]:
        return [m["Subject"].removeprefix("Orchestrator: ") for m in self.gmail.sent]

    def check(self, minutes: float = 0) -> None:
        self.clock.advance(timedelta(minutes=minutes))
        self.diagnostics.check()


@pytest.fixture
def setup() -> Setup:
    return Setup()


# --- disk ----------------------------------------------------------------------------------------


def test_a_nearly_full_disk_is_announced_once_and_when_it_is_fine_again(setup: Setup) -> None:
    setup.check()
    assert setup.gmail.sent == []
    setup.facts["disk_free_percent"] = 7.5
    setup.check()
    setup.check(5)
    assert setup.subjects() == ["Disk almost full"]
    assert "Only 7.5% of the disk is free" in setup.gmail.sent[0].get_content()
    setup.facts["disk_free_percent"] = 40.0
    setup.check()
    setup.check()
    assert setup.subjects() == ["Disk almost full", "Disk space is fine again"]


def test_exactly_ten_percent_is_not_a_problem_and_an_unknown_value_is_never_one(
    setup: Setup,
) -> None:
    setup.facts["disk_free_percent"] = 10.0
    setup.check()
    setup.facts["disk_free_percent"] = None
    setup.check()
    assert setup.gmail.sent == []


# --- temperature ---------------------------------------------------------------------------


def test_a_hot_cpu_is_announced_and_only_ends_when_clearly_cooler(setup: Setup) -> None:
    setup.facts["cpu_temp_c"] = 80.0
    setup.check()
    assert setup.gmail.sent == []  # the limit is "above 80"
    setup.facts["cpu_temp_c"] = 81.2
    setup.check()
    setup.facts["cpu_temp_c"] = 78.0  # cooler but not below 75: still the same problem
    setup.check()
    assert setup.subjects() == ["Pi is too hot"]
    setup.facts["cpu_temp_c"] = 74.0
    setup.check()
    setup.facts["cpu_temp_c"] = 79.0  # not above 80 again
    setup.check()
    assert setup.subjects() == ["Pi is too hot", "Pi has cooled down"]


# --- memory ---------------------------------------------------------------------------------------


def test_memory_must_stay_high_for_ten_minutes_before_it_is_a_problem(setup: Setup) -> None:
    setup.facts["memory_rss_mb"] = 170.0
    setup.check()
    setup.check(9)
    assert setup.gmail.sent == []
    setup.check(1)
    assert setup.subjects() == ["Memory use is high"]
    setup.facts["memory_rss_mb"] = 90.0
    setup.check()
    assert setup.subjects() == ["Memory use is high", "Memory use is normal again"]


def test_a_dip_below_the_limit_restarts_the_ten_minutes(setup: Setup) -> None:
    setup.facts["memory_rss_mb"] = 170.0
    setup.check()
    setup.check(6)
    setup.facts["memory_rss_mb"] = 120.0
    setup.check()
    setup.facts["memory_rss_mb"] = 170.0
    setup.check()
    setup.check(6)
    assert setup.gmail.sent == []
    setup.check(5)
    assert setup.subjects() == ["Memory use is high"]


# --- clock ----------------------------------------------------------------------------------------


def test_an_unsynchronized_clock_is_a_problem_after_ten_minutes(setup: Setup) -> None:
    setup.facts["clock_synchronized"] = False
    setup.check()
    setup.check(9)
    assert setup.gmail.sent == []
    setup.check(1)
    assert setup.subjects() == ["Clock is not synchronized"]
    setup.facts["clock_synchronized"] = True
    setup.check()
    assert setup.subjects()[-1] == "Clock is synchronized"


def test_a_clock_that_was_synchronized_in_time_is_never_reported(setup: Setup) -> None:
    setup.facts["clock_synchronized"] = False
    setup.check()
    setup.check(8)
    setup.facts["clock_synchronized"] = True
    setup.check(1)
    setup.facts["clock_synchronized"] = None  # cannot be asked: not a reason to alarm
    setup.check(30)
    assert setup.gmail.sent == []


# --- file pushes ---------------------------------------------------------------------------------


def test_a_schedule_file_waiting_for_fifteen_minutes_is_reported_with_the_reason(
    setup: Setup,
) -> None:
    setup.pushes = [push_status(ScheduleKind.BOILER, pending=True, error="HTTP 500")]
    setup.check()
    setup.check(14)
    assert setup.gmail.sent == []
    setup.check(1)
    assert setup.subjects() == ["Schedule file not delivered"]
    assert "boiler (HTTP 500)" in setup.gmail.sent[0].get_content()
    setup.pushes = [push_status(ScheduleKind.BOILER, pending=False)]
    setup.check()
    assert setup.subjects()[-1] == "Schedule files delivered"


def test_a_file_that_is_delivered_in_time_resets_the_wait(setup: Setup) -> None:
    setup.pushes = [push_status(ScheduleKind.TIMER, pending=True)]
    setup.check()
    setup.check(10)
    setup.pushes = [push_status(ScheduleKind.TIMER, pending=False)]
    setup.check()
    setup.pushes = [push_status(ScheduleKind.TIMER, pending=True)]
    setup.check()
    setup.check(10)
    assert setup.gmail.sent == []


# --- sending trouble ------------------------------------------------------------------------------


def test_a_notice_that_cannot_be_sent_is_retried_and_does_not_use_up_the_day() -> None:
    made = Setup(daily_max=2)
    made.facts["disk_free_percent"] = 3.0
    made.gmail.error = FakeHttpError(503)
    for _ in range(6):  # more attempts than the daily maximum
        made.check()
    made.gmail.error = None
    made.check()
    assert made.subjects() == ["Disk almost full"]


# --- the heartbeat line --------------------------------------------------------------------------


def test_the_heartbeat_comes_every_hour_with_the_key_figures(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    setup.components = [
        component("status-poller", age=2.5),
        component("calendar", Health.DEGRADED),
        component("mail", Health.FAILED),
    ]
    setup.pushes = [push_status(ScheduleKind.BOILER, pending=True)]
    with caplog.at_level(logging.INFO):
        assert setup.diagnostics.maybe_heartbeat() is False  # arms the timer at the start
        setup.clock.advance(timedelta(minutes=59))
        assert setup.diagnostics.maybe_heartbeat() is False
        setup.clock.advance(timedelta(minutes=1))
        assert setup.diagnostics.maybe_heartbeat() is True
        setup.clock.advance(timedelta(minutes=30))
        assert setup.diagnostics.maybe_heartbeat() is False
    line = next(r.message for r in caplog.records if "event=heartbeat" in r.message)
    for part in (
        "version=9.9.9",
        "status=failed",
        "uptime_s=7380",
        "rss_mb=60.0",
        "cpu_temp_c=50.0",
        "disk_free_pct=55.0",
        "clock_synchronized=True",
        "push_pending=1",
        "failed=mail",
        "degraded=calendar",
        "cycle_age_status_poller=2.5",
    ):
        assert part in line, part


def test_the_heartbeat_leaves_out_what_it_does_not_know(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    setup.facts = {}
    with caplog.at_level(logging.INFO):
        setup.diagnostics.maybe_heartbeat()
        setup.clock.advance(timedelta(hours=1))
        setup.diagnostics.maybe_heartbeat()
    line = next(r.message for r in caplog.records if "event=heartbeat" in r.message)
    assert "rss_mb" not in line and "failed" not in line and "None" not in line


# --- Home Assistant -------------------------------------------------------------------------


def test_the_values_for_home_assistant_are_rounded_so_they_do_not_change_every_second(
    setup: Setup,
) -> None:
    setup.facts.update(memory_rss_mb=61.6, cpu_temp_c=50.4)
    setup.pushes = [
        push_status(ScheduleKind.BOILER, pending=True),
        push_status(ScheduleKind.TIMER, pending=False),
    ]
    setup.components = [
        component("a"),
        component("b", Health.FAILED),
        component("c", Health.FAILED),
    ]
    setup.sync = SyncReport(NOW, True, None, False, False, "", ())
    values = setup.diagnostics.mqtt_values()
    assert values == {
        "version": "9.9.9",
        "uptime_min": 123,
        "pending_pushes": 1,
        "failed_components": 2,
        "memory_mb": 62,
        "cpu_temp_c": 50,
        "last_calendar_sync": "2026-10-24T16:41:00+03:00",
    }


def test_unknown_values_and_a_failed_calendar_read_are_left_out(setup: Setup) -> None:
    setup.facts = {}
    setup.sync = SyncReport(NOW, False, None, False, False, "", (), error="down")
    values = setup.diagnostics.mqtt_values()
    assert {"memory_mb", "cpu_temp_c", "last_calendar_sync"}.isdisjoint(values)
    assert values["version"] == "9.9.9"


# --- the thread -----------------------------------------------------------------------------------


def wait_until(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_a_bug_in_a_reader_is_logged_and_the_loop_keeps_running(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    def flaky() -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bug")
        return {"disk_free_percent": 2.0}

    diagnostics = Diagnostics(
        setup.clock,
        setup.notifier,
        facts=flaky,
        push_statuses=lambda: [],
        health=setup.report,
        sync_report=lambda: None,
        version="v",
        interval_s=0.02,
    )
    stop = threading.Event()
    with caplog.at_level(logging.ERROR):
        thread = diagnostics.start(stop)
        assert wait_until(lambda: bool(setup.gmail.sent))
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and diagnostics.last_cycle is not None
    assert "event=diagnostics_failed" in caplog.text
    assert setup.subjects() == ["Disk almost full"]
