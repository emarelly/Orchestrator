"""Manual On / Off / Auto and relay runs, end to end against the fake boiler Pi (FR-MAN-1..7)."""

import logging
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orchestrator.boiler_client import BoilerClient
from orchestrator.config import BoilerConfig, LimitsConfig
from orchestrator.logging_setup import current_correlation_id
from orchestrator.manual_commands import CommandResult, ManualCommands
from orchestrator.status_cache import StatusCache, StatusPoller
from tests.fake_boiler import FakeBoilerPi
from tests.fakes import FakeClock

JERUSALEM = ZoneInfo("Asia/Jerusalem")
# the fake boiler Pi's reading is dated Saturday 2026-10-24 16:40:19
SATURDAY = datetime(2026, 10, 24, 16, 41, 0, tzinfo=JERUSALEM)
FRIDAY = datetime(2026, 10, 23, 13, 25, 9, tzinfo=JERUSALEM)
SENTINEL_WHO = "ui:alice"


class Rig:
    def __init__(
        self,
        *,
        start: datetime = SATURDAY,
        dry_run: bool = False,
        config: BoilerConfig | None = None,
        poll_first: bool = True,
    ) -> None:
        self.pi = FakeBoilerPi()
        self.clock = FakeClock(start)
        self.cache = StatusCache()
        self.session = self.pi.session()
        client_config = config or self.pi.config
        self.client = BoilerClient(client_config, session=self.session, dry_run=dry_run)
        self.poller = StatusPoller(self.client, self.cache, self.clock, self.pi.config)
        self.commands = ManualCommands(
            self.client,
            self.cache,
            self.clock,
            LimitsConfig(),
            stale_after=timedelta(minutes=5),
            refresh=self.poller.poll_once,
        )
        if poll_first:
            self.poller.poll_once()

    def posts(self) -> list[tuple[str, str]]:
        return [call for call in self.pi.calls if call[0] == "POST"]


@pytest.fixture
def rig() -> Iterator[Rig]:
    made = Rig()
    yield made
    made.session.close()


def on(rig: Rig) -> CommandResult:
    return rig.commands.manual_on(source="ui", who=SENTINEL_WHO)


# --- manual On --------------------------------------------------------------------------------


def test_manual_on_uploads_the_expected_file_and_confirms_the_mode(rig: Rig) -> None:
    result = on(rig)
    assert result.ok and result.sent
    assert result.filename == "manual.json"
    # 4.92 showers + 1.0 would be above the heater limit, so the level is the cap, 5.0;
    # it expires 3 hours after 16:41:00 on Saturday (day 7)
    assert rig.pi.files["manual.json"] == (
        b'[{"DayOfWeek":7,"Hour":"19:41:00","Target":5.0,"Min":5.0}]'
    )
    assert result.observed_mode == "Manual"
    assert "Target and Min 5.0, until Saturday 19:41:00" in result.message
    assert "now reports Manual" in result.message
    assert len(rig.posts()) == 1


def test_manual_on_uses_the_level_from_the_status_when_it_is_below_the_cap(rig: Rig) -> None:
    rig.pi.status.update(CurrentShowers=1.2, CurrentShowersnew=2.31)
    rig.poller.poll_once()
    on(rig)
    assert rig.pi.files["manual.json"].startswith(
        b'[{"DayOfWeek":7,"Hour":"19:41:00","Target":3.4,'
    )


def test_manual_off_uploads_zero_zero(rig: Rig) -> None:
    result = rig.commands.manual_off(source="mqtt", who="ha")
    assert result.ok and result.sent
    assert rig.pi.files["manual.json"] == b'[{"DayOfWeek":7,"Hour":"19:41:00","Target":0,"Min":0}]'


def test_auto_removes_the_override_again(rig: Rig) -> None:
    on(rig)
    result = rig.commands.auto(source="email", who="owner@example.com")
    assert result.ok and result.sent
    assert "manual.json" not in rig.pi.files
    assert result.observed_mode == "Auto"
    assert rig.pi.uploads[-1].content == b"[]"


# --- refusals: nothing is sent ----------------------------------------------------------------


def test_manual_on_is_refused_when_the_status_is_unknown() -> None:
    cold = Rig(poll_first=False)
    result = on(cold)
    assert not result.ok and not result.sent
    assert "status is unknown" in result.message
    assert cold.posts() == []
    cold.session.close()


def test_manual_on_is_refused_when_the_reading_is_stale(rig: Rig) -> None:
    rig.clock.advance(timedelta(minutes=10))
    result = on(rig)
    assert not result.ok
    assert "too old" in result.message
    assert rig.posts() == []


def test_manual_on_is_refused_when_the_boiler_pi_is_down(rig: Rig) -> None:
    rig.pi.offline = True
    for _ in range(3):
        rig.poller.poll_once()
    result = on(rig)
    assert not result.ok and "not responding" in result.message
    assert rig.posts() == []


def test_manual_off_and_auto_do_not_need_a_status() -> None:
    cold = Rig(poll_first=False)
    assert cold.commands.manual_off(source="ui", who="x").ok
    assert cold.commands.auto(source="ui", who="x").ok
    cold.session.close()


def test_a_missing_upload_path_is_reported_and_nothing_is_sent() -> None:
    undecided = Rig(config=BoilerConfig())  # no manual upload paths configured (OQ-16)
    result = on(undecided)
    assert not result.ok and not result.sent
    assert "not configured" in result.message and "OQ-16" in result.message
    assert undecided.posts() == []
    undecided.session.close()


# --- failures: exactly one attempt, no retry --------------------------------------------------


def test_a_rejected_upload_is_reported_and_not_retried(rig: Rig) -> None:
    rig.pi.reject_uploads_with = 503
    result = on(rig)
    assert not result.ok and not result.sent
    assert "not sent" in result.message and "503" in result.message
    assert len(rig.posts()) == 1


def test_an_unreachable_boiler_pi_at_upload_time_is_reported_and_not_retried(rig: Rig) -> None:
    rig.pi.offline = True
    result = on(rig)  # the cache still holds the fresh reading from before
    assert not result.ok and "not sent" in result.message
    assert len(rig.posts()) == 1


# --- dry run ----------------------------------------------------------------------------------


def test_in_dry_run_the_file_is_built_but_nothing_is_posted() -> None:
    dry = Rig(dry_run=True)
    result = on(dry)
    assert result.ok and not result.sent
    assert "dry run" in result.message
    assert dry.posts() == []
    assert "manual.json" not in dry.pi.files
    dry.session.close()


# --- relay runs -------------------------------------------------------------------------------


def test_relay_on_uploads_the_documented_file() -> None:
    friday = Rig(start=FRIDAY)
    result = friday.commands.relay_on(2, 150, source="email", who="owner@example.com")
    assert result.ok and result.sent and result.filename == "2Timermanual.json"
    assert friday.pi.files["2Timermanual.json"] == (
        b'[{"DayOfWeek":6,"Hour":"13:25:09","State":1,"RelayNum":2},'
        b'{"DayOfWeek":6,"Hour":"13:27:39","State":0,"RelayNum":2}]'
    )
    friday.session.close()


@pytest.mark.parametrize(("relay", "seconds"), [(0, 600), (1, 600), (8, 600), (4, 10), (4, 90000)])
def test_relay_requests_outside_the_rules_are_refused(rig: Rig, relay: int, seconds: int) -> None:
    result = rig.commands.relay_on(relay, seconds, source="mqtt", who="ha")
    assert not result.ok and not result.sent
    assert rig.posts() == []


def test_relay_off_uploads_a_single_off_entry() -> None:
    friday = Rig(start=FRIDAY)
    result = friday.commands.relay_off(5, source="ui", who="x")
    assert result.ok
    assert friday.pi.files["5Timermanual.json"] == (
        b'[{"DayOfWeek":6,"Hour":"13:25:09","State":0,"RelayNum":5}]'
    )
    friday.session.close()


def test_relays_0_and_1_can_never_be_switched_off_manually_either(rig: Rig) -> None:
    for relay in (0, 1):
        assert not rig.commands.relay_off(relay, source="ui", who="x").ok
    assert rig.posts() == []


# --- audit trail and correlation id -----------------------------------------------------------


def test_every_attempt_is_audited_with_who_channel_and_result(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    ok = on(rig)
    rig.pi.reject_uploads_with = 500
    failed = rig.commands.manual_off(source="mqtt", who="ha")
    rig.commands.relay_on(1, 600, source="email", who="owner@example.com")  # refused
    audit_lines = [r.getMessage() for r in caplog.records if r.name == "orchestrator.audit"]
    assert len(audit_lines) == 3
    assert f"event=manual_on who={SENTINEL_WHO} channel=ui result=ok" in audit_lines[0]
    assert "file=manual.json observed_mode=Manual" in audit_lines[0]
    assert "event=manual_off who=ha channel=mqtt result=failed" in audit_lines[1]
    assert "event=relay_on who=owner@example.com channel=email result=refused" in audit_lines[2]
    assert "relay=1 seconds=600" in audit_lines[2]
    assert ok.correlation_id and failed.correlation_id != ok.correlation_id


class _CorrelationRecorder(logging.Handler):
    """Remembers, for every record, which correlation id was active when it was logged."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.seen: list[tuple[str, str | None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append((record.name, current_correlation_id()))


def test_the_correlation_id_ties_the_log_lines_of_one_command_together(rig: Rig) -> None:
    recorder = _CorrelationRecorder()
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(recorder)
    try:
        result = on(rig)
    finally:
        root.removeHandler(recorder)
        root.setLevel(previous_level)
    by_logger = {name: cid for name, cid in recorder.seen if name.startswith("orchestrator.")}
    for name in (
        "orchestrator.boiler_client",
        "orchestrator.audit",
        "orchestrator.manual_commands",
    ):
        assert by_logger[name] == result.correlation_id, name
    assert current_correlation_id() is None  # and it does not leak out of the command


# --- concurrency ------------------------------------------------------------------------------


def test_commands_from_different_channels_never_interleave(rig: Rig) -> None:
    results: list[CommandResult] = []
    errors: list[BaseException] = []

    def run(action: str) -> None:
        try:
            if action == "on":
                results.append(on(rig))
            else:
                results.append(rig.commands.manual_off(source="mqtt", who="ha"))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run, args=(a,)) for a in ("on", "off") * 5]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert len(results) == 10 and all(r.ok for r in results)
    assert len(rig.posts()) == 10
    assert len({r.correlation_id for r in results}) == 10
