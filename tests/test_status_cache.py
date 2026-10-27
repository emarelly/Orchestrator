"""Status cache and poller (FR-STA-1..5, CS-11, CS-12), against the fake boiler Pi."""

import dataclasses
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orchestrator.boiler_client import BoilerClient
from orchestrator.config import BoilerConfig, ConfigError, load_config
from orchestrator.models import BoilerStatus
from orchestrator.status_cache import (
    ConnectionState,
    StatusCache,
    StatusPoller,
    Transition,
)
from tests.fake_boiler import DEFAULT_STATUS, FakeBoilerPi
from tests.fakes import FakeClock

JERUSALEM = ZoneInfo("Asia/Jerusalem")
READING_TIME = datetime(2026, 10, 24, 16, 40, 19)  # the Date inside the fake's status
STALE_AFTER = timedelta(minutes=5)


class Rig:
    """A fake boiler Pi, a client, a cache, a fake clock and a poller wired together."""

    def __init__(self, down_after_failures: int = 3) -> None:
        self.pi = FakeBoilerPi()
        self.clock = FakeClock(datetime(2026, 10, 24, 16, 41, 0, tzinfo=JERUSALEM))
        self.cache = StatusCache()
        self.transitions: list[Transition] = []
        self.session = self.pi.session()
        config = dataclasses.replace(self.pi.config, down_after_failures=down_after_failures)
        self.poller = StatusPoller(
            BoilerClient(config, session=self.session),
            self.cache,
            self.clock,
            config,
            on_transition=self.transitions.append,
        )

    def fresh(self) -> BoilerStatus | None:
        return self.cache.fresh_status(self.clock.now(), STALE_AFTER)


@pytest.fixture
def rig() -> Iterator[Rig]:
    made = Rig()
    yield made
    made.session.close()


# --- good readings ----------------------------------------------------------------------------


def test_the_first_good_reading_is_stored_and_reported_as_a_transition(rig: Rig) -> None:
    transition = rig.poller.poll_once()
    snapshot = rig.cache.snapshot()
    assert snapshot.state is ConnectionState.UP
    assert snapshot.status is not None and snapshot.status.mode == "Auto"
    assert snapshot.fetched_at == rig.clock.now()
    assert snapshot.error is None and snapshot.consecutive_failures == 0
    assert transition == Transition(
        ConnectionState.UNKNOWN, ConnectionState.UP, rig.clock.now(), None, None
    )
    assert rig.transitions == [transition]


def test_more_good_readings_are_not_reported_again(rig: Rig) -> None:
    rig.poller.poll_once()
    assert rig.poller.poll_once() is None
    assert len(rig.transitions) == 1


def test_a_snapshot_is_immutable_and_unaffected_by_later_polls(rig: Rig) -> None:
    rig.poller.poll_once()
    before = rig.cache.snapshot()
    rig.pi.status["Status"] = "Manual"
    rig.clock.advance(timedelta(seconds=10))
    rig.poller.poll_once()
    assert before.status is not None and before.status.mode == "Auto"
    assert rig.cache.snapshot().status is not None
    field_name = "state"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(before, field_name, ConnectionState.DOWN)


# --- failures: down after several in a row, nothing invented -------------------------------------


def test_one_or_two_failures_do_not_make_the_boiler_down_but_keep_the_last_reading(
    rig: Rig,
) -> None:
    rig.poller.poll_once()
    good = rig.cache.snapshot().status
    rig.pi.offline = True
    for expected in (1, 2):
        assert rig.poller.poll_once() is None
        snapshot = rig.cache.snapshot()
        assert snapshot.state is ConnectionState.UP
        assert snapshot.consecutive_failures == expected
        assert snapshot.status == good  # the last good reading, not zeros
        assert snapshot.error is not None


def test_the_third_failure_in_a_row_reports_down_once(rig: Rig) -> None:
    rig.poller.poll_once()
    rig.pi.offline = True
    results = [rig.poller.poll_once() for _ in range(6)]
    transitions = [t for t in results if t is not None]
    assert len(transitions) == 1
    assert (transitions[0].previous, transitions[0].current) == (
        ConnectionState.UP,
        ConnectionState.DOWN,
    )
    assert transitions[0].error is not None
    assert rig.cache.snapshot().consecutive_failures == 6
    assert len(rig.transitions) == 2  # first reading + down


def test_a_good_reading_between_failures_resets_the_count(rig: Rig) -> None:
    rig.poller.poll_once()
    rig.pi.offline = True
    rig.poller.poll_once()
    rig.poller.poll_once()
    rig.pi.offline = False
    rig.poller.poll_once()
    assert rig.cache.snapshot().consecutive_failures == 0
    rig.pi.offline = True
    rig.poller.poll_once()
    rig.poller.poll_once()
    assert rig.cache.snapshot().state is ConnectionState.UP


def test_coming_back_reports_up_once_with_the_length_of_the_outage(rig: Rig) -> None:
    rig.poller.poll_once()
    rig.pi.offline = True
    for _ in range(3):
        rig.poller.poll_once()
    rig.clock.advance(timedelta(minutes=5))
    rig.pi.offline = False
    transition = rig.poller.poll_once()
    assert transition is not None
    assert (transition.previous, transition.current) == (ConnectionState.DOWN, ConnectionState.UP)
    assert transition.outage == timedelta(minutes=5)
    assert rig.poller.poll_once() is None


def test_a_boiler_pi_that_was_never_reached_has_no_status_at_all(rig: Rig) -> None:
    rig.pi.offline = True
    for _ in range(3):
        rig.poller.poll_once()
    snapshot = rig.cache.snapshot()
    assert snapshot.state is ConnectionState.DOWN
    assert snapshot.status is None and snapshot.fetched_at is None  # nothing is made up
    assert rig.fresh() is None


@pytest.mark.parametrize("failure", ["http500", "garbage", "bad_status"])
def test_every_kind_of_failure_counts(rig: Rig, failure: str) -> None:
    rig.poller.poll_once()
    for _ in range(3):
        if failure == "http500":
            rig.pi.fail_next(1, 500)
        elif failure == "garbage":
            rig.pi.garbage_status = True
        else:
            rig.pi.status = {k: v for k, v in DEFAULT_STATUS.items() if k != "CurrentTemp"}
        rig.poller.poll_once()
    assert rig.cache.snapshot().state is ConnectionState.DOWN


def test_down_after_one_failure_when_configured() -> None:
    quick = Rig(down_after_failures=1)
    quick.pi.offline = True
    transition = quick.poller.poll_once()
    assert transition is not None and transition.current is ConnectionState.DOWN
    quick.session.close()


def test_an_outage_across_the_clock_change_is_measured_in_real_time() -> None:
    # down at 01:30 (+03:00) on the night the clocks go back; two real hours later the wall clock
    # shows 02:30 (+02:00), only one hour after 01:30 - the outage is still two hours
    night = Rig()
    night.clock = FakeClock(datetime(2026, 10, 25, 1, 30, 0, tzinfo=JERUSALEM))
    night.poller = StatusPoller(
        BoilerClient(night.pi.config, session=night.session),
        night.cache,
        night.clock,
        night.pi.config,
    )
    night.poller.poll_once()
    night.pi.offline = True
    for _ in range(3):
        night.poller.poll_once()
    night.clock.advance(timedelta(hours=2))
    night.pi.offline = False
    transition = night.poller.poll_once()
    assert transition is not None and transition.outage == timedelta(hours=2)
    night.session.close()


# --- a boiler service that answers but is not active -------------------------------------------


def test_active_no_is_its_own_unhealthy_state_and_recovers(rig: Rig) -> None:
    rig.poller.poll_once()
    rig.pi.status["ActiveYesNo"] = "No"
    inactive = rig.poller.poll_once()
    assert inactive is not None and inactive.current is ConnectionState.INACTIVE
    assert rig.cache.snapshot().status is not None  # it still answers
    assert rig.poller.poll_once() is None
    rig.clock.advance(timedelta(minutes=2))
    rig.pi.status["ActiveYesNo"] = "Yes"
    back = rig.poller.poll_once()
    assert back is not None and back.current is ConnectionState.UP
    assert back.outage == timedelta(minutes=2)


# --- stale readings (judged by the reading's own Date) -----------------------------------------


def test_a_fresh_reading_is_usable_and_an_old_one_is_stale(rig: Rig) -> None:
    rig.poller.poll_once()
    assert rig.fresh() is not None  # 41 seconds old
    rig.clock.advance(timedelta(minutes=10))
    rig.poller.poll_once()  # the Pi answers, but with the same old Date
    assert rig.cache.snapshot().state is ConnectionState.UP
    assert rig.cache.snapshot().is_stale(rig.clock.now().replace(tzinfo=None), STALE_AFTER)
    assert rig.fresh() is None


def test_the_reading_gets_stale_while_the_boiler_pi_is_unreachable(rig: Rig) -> None:
    rig.poller.poll_once()
    rig.pi.offline = True
    rig.poller.poll_once()
    assert rig.fresh() is not None  # one failure: still the same fresh reading
    rig.clock.advance(timedelta(minutes=6))
    assert rig.fresh() is None


def test_fresh_status_is_none_when_down_even_if_the_old_reading_is_recent(rig: Rig) -> None:
    rig.poller.poll_once()
    rig.pi.offline = True
    for _ in range(3):
        rig.poller.poll_once()
    assert rig.cache.snapshot().status is not None
    assert rig.fresh() is None  # a down boiler Pi must not drive a manual action


# --- logging: one line per transition, not per poll ---------------------------------------------


def test_down_and_up_are_each_logged_once(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig.poller.poll_once()
    rig.pi.offline = True
    for _ in range(10):
        rig.poller.poll_once()
    rig.clock.advance(timedelta(seconds=90))
    rig.pi.offline = False
    rig.poller.poll_once()
    messages = [r.getMessage() for r in caplog.records if r.name == "orchestrator.status_cache"]
    assert sum("event=boiler_down" in m for m in messages) == 1
    assert sum("event=boiler_up" in m and "outage_s=90" in m for m in messages) == 1
    assert sum("event=boiler_first_reading" in m for m in messages) == 1


# --- robustness (CS-11) -------------------------------------------------------------------------


def test_a_failing_transition_handler_does_not_break_polling(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_transition: Transition) -> None:
        raise RuntimeError("handler bug")

    poller = StatusPoller(
        BoilerClient(rig.pi.config, session=rig.session),
        rig.cache,
        rig.clock,
        rig.pi.config,
        on_transition=broken,
    )
    assert poller.poll_once() is not None
    assert poller.poll_once() is None
    assert "transition_handler_failed" in caplog.text


class Flaky:
    """A status source that crashes once with an unexpected error, then works."""

    def __init__(self, good: BoilerStatus) -> None:
        self.good = good
        self.calls = 0

    def get_status(self) -> BoilerStatus:
        self.calls += 1
        if self.calls == 1:
            raise ValueError("unexpected bug")
        return self.good


def wait_until(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_the_loop_survives_an_unexpected_error_and_stops_on_request(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    source = Flaky(BoilerStatus.parse(DEFAULT_STATUS))
    poller = StatusPoller(source, rig.cache, rig.clock, BoilerConfig(), interval_s=0.01)
    stop = threading.Event()
    thread = poller.start(stop)
    try:
        assert wait_until(lambda: source.calls >= 3)
        assert rig.cache.snapshot().state is ConnectionState.UP
        assert "status_poll_crashed" in caplog.text
        assert poller.last_cycle is not None
    finally:
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert thread.daemon and thread.name == "status-poller"


# --- thread safety (CS-12) ----------------------------------------------------------------------


def test_the_cache_stays_consistent_under_concurrent_use() -> None:
    cache = StatusCache()
    clock_now = datetime(2026, 10, 24, 16, 41, 0, tzinfo=JERUSALEM)
    good = BoilerStatus.parse(DEFAULT_STATUS)
    errors: list[BaseException] = []

    def writer(use_failures: bool) -> None:
        try:
            for _ in range(300):
                if use_failures:
                    cache.record_failure("boom", clock_now, 3)
                else:
                    cache.record_success(good, clock_now)
        except BaseException as error:  # collected and asserted below
            errors.append(error)

    def reader() -> None:
        try:
            for _ in range(600):
                snapshot = cache.snapshot()
                assert snapshot.consecutive_failures >= 0
                assert snapshot.state in tuple(ConnectionState)
                if snapshot.state is ConnectionState.UP:
                    assert snapshot.status is not None
        except BaseException as error:
            errors.append(error)

    threads: list[threading.Thread] = [
        threading.Thread(target=writer, args=(flag,)) for flag in (True, False, True, False)
    ] + [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []


# --- configuration ------------------------------------------------------------------------------


def test_the_new_boiler_settings_have_defaults_and_limits(tmp_path: Any) -> None:
    config = load_config(None, env={})
    assert config.boiler.down_after_failures == 3
    assert config.boiler.stale_after_s == 300.0
    for text in ("[boiler]\ndown_after_failures = 0\n", "[boiler]\nstale_after_s = 5\n"):
        path = tmp_path / "config.toml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match=r"\[boiler\]"):
            load_config(path, env={})
