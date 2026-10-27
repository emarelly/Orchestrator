"""Atomic file writing and the push queue (FR-PUSH-1..5, CS-10, CS-14, OPS-5)."""

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orchestrator.boiler_client import BoilerClient
from orchestrator.files import sha256_hex, write_atomic
from orchestrator.models import BoilerSchedule, BoilerSlot, TimerEntry, TimerSchedule
from orchestrator.push_queue import (
    BackoffPolicy,
    Outcome,
    PushQueue,
    ScheduleKind,
)
from orchestrator.schedule_files import serialize_boiler_schedule, serialize_timer_schedule
from orchestrator.validation import ValidationError
from tests.fake_boiler import FakeBoilerPi
from tests.fakes import FakeClock

JERUSALEM = ZoneInfo("Asia/Jerusalem")
BOILER = BoilerSchedule.of(
    [BoilerSlot.create(1, "06:00:00", "3.6", "2.0"), BoilerSlot.create(1, "18:00:00", "3.0", "2.0")]
)
BOILER_2 = BoilerSchedule.of([BoilerSlot.create(2, "06:00:00", "4.0", "2.0")])
TIMER = TimerSchedule.of(
    [TimerEntry(1, clock_time(17, 30), 1, 4), TimerEntry(1, clock_time(23), 0, 4)]
)
TIMER_2 = TimerSchedule.of(
    [TimerEntry(2, clock_time(17, 30), 1, 4), TimerEntry(2, clock_time(23), 0, 4)]
)
POSTS = ("POST", "/updatecalander/")
TIMER_POSTS = ("POST", "/updaterelaycalander/")


# --- atomic file writing ----------------------------------------------------------------------


def test_write_atomic_creates_updates_and_skips_unchanged_content(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "Cal.json"
    assert write_atomic(target, b"one") is True
    assert target.read_bytes() == b"one"
    assert not target.with_name("Cal.json.bak").exists()  # nothing to back up yet
    assert write_atomic(target, b"one") is False  # unchanged: no write
    assert write_atomic(target, b"two") is True
    assert target.read_bytes() == b"two"
    assert target.with_name("Cal.json.bak").read_bytes() == b"one"  # previous version kept
    assert sorted(p.name for p in target.parent.iterdir()) == ["Cal.json", "Cal.json.bak"]


def test_write_atomic_leaves_the_old_file_intact_if_the_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Cal.json"
    write_atomic(target, b"good")

    def broken_replace(_source: object, _destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(target, b"new content")
    assert target.read_bytes() == b"good"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []  # no litter


# --- the backoff policy -----------------------------------------------------------------------


def test_backoff_doubles_up_to_the_cap_with_jitter() -> None:
    policy = BackoffPolicy(base_s=10, factor=2, max_s=300, jitter=0.2)
    middle = [policy.delay(n, 0.5).total_seconds() for n in range(1, 8)]
    assert middle == [10, 20, 40, 80, 160, 300, 300]  # random value 0.5 = no jitter
    assert policy.delay(1, 0.0).total_seconds() == 8  # -20 %
    assert policy.delay(1, 1.0).total_seconds() == 12  # +20 %


# --- the queue ----------------------------------------------------------------------------------


class Rig:
    def __init__(
        self, directory: Path, *, dry_run: bool = False, gate: Callable[[], bool] | None = None
    ) -> None:
        self.directory = directory
        self.pi = FakeBoilerPi()
        self.clock = FakeClock(datetime(2026, 10, 24, 16, 41, 0, tzinfo=JERUSALEM))
        self.session = self.pi.session()
        self.client = BoilerClient(self.pi.config, session=self.session, dry_run=dry_run)
        self.gate_open = True
        self.queue = self.make_queue(gate)

    def make_queue(
        self, gate: Callable[[], bool] | None = None, client: BoilerClient | None = None
    ) -> PushQueue:
        return PushQueue(
            client or self.client,
            self.directory,
            self.clock,
            gate=gate or (lambda: self.gate_open),
            jitter_source=lambda: 0.5,  # deterministic: no jitter
            poll_interval_s=0.01,
        )

    def posts(self) -> list[tuple[str, str]]:
        return [call for call in self.pi.calls if call[0] == "POST"]


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path / "data")
    yield made
    made.session.close()


def test_a_new_schedule_is_saved_locally_and_pushed_once(rig: Rig) -> None:
    result = rig.queue.set_boiler_schedule(BOILER)
    assert result.changed_locally and result.pending
    assert (rig.directory / "Cal.json").read_bytes() == serialize_boiler_schedule(BOILER)
    outcomes = {r.kind: r.outcome for r in rig.queue.process()}
    assert outcomes == {ScheduleKind.BOILER: Outcome.PUSHED, ScheduleKind.TIMER: Outcome.SKIPPED}
    assert rig.pi.files["Cal.json"] == serialize_boiler_schedule(BOILER)
    assert rig.posts() == [POSTS]
    rig.queue.process()
    assert rig.posts() == [POSTS]  # nothing pending: nothing sent again


def test_submitting_the_same_schedule_again_changes_nothing(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.queue.process()
    again = rig.queue.set_boiler_schedule(BOILER)
    assert not again.changed_locally and not again.pending
    rig.queue.process()
    assert rig.posts() == [POSTS]


def test_only_the_schedule_that_changed_is_sent(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.queue.set_timer_schedule(TIMER)
    rig.queue.process()
    assert rig.posts() == [POSTS, TIMER_POSTS]
    rig.queue.set_timer_schedule(TIMER_2)
    rig.queue.process()
    assert rig.posts() == [POSTS, TIMER_POSTS, TIMER_POSTS]  # the boiler schedule was not resent
    assert rig.pi.files["TimeCal.json"] == serialize_timer_schedule(TIMER_2)


def test_the_previous_version_is_kept_as_a_backup(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.queue.set_boiler_schedule(BOILER_2)
    assert (rig.directory / "Cal.json.bak").read_bytes() == serialize_boiler_schedule(BOILER)


def test_invalid_or_empty_schedules_are_refused_and_nothing_is_written(rig: Rig) -> None:
    too_hot = BoilerSchedule.of([BoilerSlot.create(1, "06:00:00", "6", "2")])
    with pytest.raises(ValidationError, match="above the limit"):
        rig.queue.set_boiler_schedule(too_hot)
    with pytest.raises(ValidationError, match="no slots"):
        rig.queue.set_boiler_schedule(BoilerSchedule())
    with pytest.raises(ValidationError, match="no entries"):
        rig.queue.set_timer_schedule(TimerSchedule())
    assert not rig.directory.exists() or list(rig.directory.iterdir()) == []
    assert rig.posts() == []


# --- failures and retries -----------------------------------------------------------------------


def test_failures_are_retried_with_growing_delays_until_it_works(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.pi.offline = True
    waits = []
    for _ in range(3):
        outcome = rig.queue.process()[0]
        assert outcome.outcome is Outcome.FAILED and outcome.error
        status = rig.queue.statuses()[0]
        assert status.pending and status.next_attempt_at is not None
        waits.append(status.next_attempt_at - rig.clock.now())
        rig.clock.advance(waits[-1])
    assert waits == [timedelta(seconds=10), timedelta(seconds=20), timedelta(seconds=40)]
    assert rig.queue.statuses()[0].attempts == 3

    rig.pi.offline = False
    assert rig.queue.process()[0].outcome is Outcome.PUSHED
    status = rig.queue.statuses()[0]
    assert not status.pending and status.attempts == 0 and status.last_error is None
    assert status.last_pushed_at == rig.clock.now()


def test_nothing_is_sent_before_the_retry_time(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.pi.offline = True
    rig.queue.process()
    calls = len(rig.pi.calls)
    rig.clock.advance(timedelta(seconds=9))
    assert rig.queue.process()[0].outcome is Outcome.SKIPPED
    assert len(rig.pi.calls) == calls
    rig.clock.advance(timedelta(seconds=1))
    assert rig.queue.process()[0].outcome is Outcome.FAILED
    assert len(rig.pi.calls) == calls + 1


def test_a_rejected_upload_is_not_marked_as_pushed(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.pi.reject_uploads_with = 503
    assert rig.queue.process()[0].outcome is Outcome.FAILED
    status = rig.queue.statuses()[0]
    assert status.pending and status.last_pushed_at is None
    assert status.last_error is not None and "503" in status.last_error


def test_a_new_schedule_is_tried_immediately_even_during_a_backoff(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.pi.offline = True
    rig.queue.process()
    rig.pi.offline = False
    rig.queue.set_boiler_schedule(BOILER_2)  # the content changed: no point waiting
    assert rig.queue.process()[0].outcome is Outcome.PUSHED
    assert rig.pi.files["Cal.json"] == serialize_boiler_schedule(BOILER_2)


def test_a_retry_is_not_delayed_by_an_hour_when_the_clocks_go_back(rig: Rig) -> None:
    # 2026-10-25 02:00 (+03:00) becomes 01:00 (+02:00); a failure at 01:59:55 must be retried
    # 10 real seconds later, not an hour later (wall-clock arithmetic would get this wrong)
    rig.clock = FakeClock(datetime(2026, 10, 25, 1, 59, 55, tzinfo=JERUSALEM))
    queue = PushQueue(
        rig.client,
        rig.directory,
        rig.clock,
        gate=lambda: True,
        jitter_source=lambda: 0.5,
        poll_interval_s=0.01,
    )
    queue.set_boiler_schedule(BOILER)
    rig.pi.offline = True
    assert queue.process()[0].outcome is Outcome.FAILED
    rig.clock.advance(timedelta(seconds=10))
    rig.pi.offline = False
    assert queue.process()[0].outcome is Outcome.PUSHED


def test_the_backoff_wait_is_capped(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.pi.offline = True
    last_wait = timedelta()
    for _ in range(9):
        rig.queue.process()
        status = rig.queue.statuses()[0]
        assert status.next_attempt_at is not None
        last_wait = status.next_attempt_at - rig.clock.now()
        rig.clock.advance(last_wait)
    assert last_wait == timedelta(seconds=300)


# --- the gate (boiler Pi down) -----------------------------------------------------------------


def test_nothing_is_sent_while_the_gate_is_closed_and_no_attempt_is_counted(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.gate_open = False
    assert rig.queue.process()[0].outcome is Outcome.SKIPPED
    assert rig.pi.calls == []
    assert rig.queue.statuses()[0].attempts == 0
    rig.gate_open = True
    assert rig.queue.process()[0].outcome is Outcome.PUSHED


# --- restart behaviour (OPS-5) -------------------------------------------------------------------


def test_after_a_restart_an_accepted_schedule_is_not_sent_again(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.queue.process()
    restarted = rig.make_queue()
    assert not restarted.statuses()[0].pending
    restarted.process()
    assert rig.posts() == [POSTS]
    state = json.loads((rig.directory / "push_state.json").read_text(encoding="utf-8"))
    assert state["boiler"]["sha256"] == sha256_hex(serialize_boiler_schedule(BOILER))


def test_after_a_restart_an_unaccepted_schedule_is_still_pending(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.pi.offline = True
    rig.queue.process()
    restarted = rig.make_queue()
    assert restarted.statuses()[0].pending  # not marked as pushed before the Pi said 2xx
    rig.pi.offline = False
    assert restarted.process()[0].outcome is Outcome.PUSHED


def test_a_local_file_changed_behind_our_back_is_pushed_after_a_restart(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.queue.process()
    (rig.directory / "Cal.json").write_bytes(serialize_boiler_schedule(BOILER_2))
    restarted = rig.make_queue()
    assert restarted.statuses()[0].pending
    assert restarted.process()[0].outcome is Outcome.PUSHED


def test_an_unreadable_state_file_means_push_everything_again(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.queue.process()
    (rig.directory / "push_state.json").write_text("{ not json", encoding="utf-8")
    assert rig.make_queue().statuses()[0].pending


# --- push now ---------------------------------------------------------------------------------


def test_push_now_resends_both_files_ignoring_change_detection_and_backoff(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    rig.queue.set_timer_schedule(TIMER)
    rig.queue.process()
    assert len(rig.posts()) == 2
    results = rig.queue.push_now()
    assert [r.outcome for r in results] == [Outcome.PUSHED, Outcome.PUSHED]
    assert len(rig.posts()) == 4
    rig.queue.process()
    assert len(rig.posts()) == 4  # and it is not repeated afterwards


def test_push_now_skips_a_schedule_that_does_not_exist_yet(rig: Rig) -> None:
    rig.queue.set_boiler_schedule(BOILER)
    results = {r.kind: r.outcome for r in rig.queue.push_now()}
    assert results == {ScheduleKind.BOILER: Outcome.PUSHED, ScheduleKind.TIMER: Outcome.SKIPPED}


# --- dry run -------------------------------------------------------------------------------------


def test_in_dry_run_nothing_is_sent_and_nothing_counts_as_pushed(tmp_path: Path) -> None:
    dry = Rig(tmp_path / "data", dry_run=True)
    dry.queue.set_boiler_schedule(BOILER)
    assert dry.queue.process()[0].outcome is Outcome.DRY_RUN
    status = dry.queue.statuses()[0]
    assert status.pending and status.last_error is None and status.attempts == 0
    assert dry.pi.calls == []
    assert not (dry.directory / "push_state.json").exists()

    live = BoilerClient(dry.pi.config, session=dry.session)  # the same data folder, now for real
    assert dry.make_queue(client=live).process()[0].outcome is Outcome.PUSHED
    dry.session.close()


# --- reporting, logging, threads --------------------------------------------------------------


def test_statuses_describe_both_schedules(rig: Rig) -> None:
    before = {s.kind: s for s in rig.queue.statuses()}
    assert (
        not before[ScheduleKind.BOILER].has_local_file and not before[ScheduleKind.BOILER].pending
    )
    rig.queue.set_boiler_schedule(BOILER)
    after = {s.kind: s for s in rig.queue.statuses()}
    assert after[ScheduleKind.BOILER].has_local_file and after[ScheduleKind.BOILER].pending
    assert not after[ScheduleKind.TIMER].has_local_file


def test_a_push_is_logged_and_audited_without_the_file_content(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    rig.queue.set_boiler_schedule(BOILER)
    rig.queue.process()
    assert "event=schedule_pushed kind=boiler" in caplog.text
    assert "who=orchestrator result=ok" in caplog.text
    assert "DayOfWeek" not in caplog.text


def wait_until(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_the_worker_thread_pushes_a_submitted_schedule_and_stops_on_request(rig: Rig) -> None:
    stop = threading.Event()
    thread = rig.queue.start(stop)
    try:
        rig.queue.set_boiler_schedule(BOILER)
        assert wait_until(lambda: "Cal.json" in rig.pi.files)
    finally:
        stop.set()
        rig.queue.wake()
        thread.join(timeout=3)
    assert not thread.is_alive() and thread.daemon and thread.name == "push-queue"


def test_concurrent_submissions_never_corrupt_the_local_file(rig: Rig) -> None:
    schedules = [BOILER, BOILER_2]
    errors: list[BaseException] = []

    def submit(schedule: BoilerSchedule) -> None:
        try:
            for _ in range(15):
                rig.queue.set_boiler_schedule(schedule)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=submit, args=(s,)) for s in schedules * 2]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    final = (rig.directory / "Cal.json").read_bytes()
    assert final in (serialize_boiler_schedule(BOILER), serialize_boiler_schedule(BOILER_2))
