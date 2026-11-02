"""Manual On / Off / Auto and relay runs, end to end against the fake boiler Pi (FR-MAN-1..7)."""

import logging
import threading
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orchestrator.boiler_client import BoilerClient
from orchestrator.logging_setup import current_correlation_id
from orchestrator.manual_commands import CommandResult, ManualCommands
from orchestrator.status_cache import StatusCache, StatusPoller
from tests.fake_boiler import FakeBoilerPi
from tests.fakes import FakeClock

JERUSALEM = ZoneInfo("Asia/Jerusalem")
# the fake boiler Pi's reading is dated Saturday 2026-10-24 16:40:19
SATURDAY = datetime(2026, 10, 24, 16, 41, 0, tzinfo=JERUSALEM)
SENTINEL_WHO = "ui:alice"


class Rig:
    def __init__(
        self, *, start: datetime = SATURDAY, dry_run: bool = False, poll_first: bool = True
    ) -> None:
        self.pi = FakeBoilerPi()
        self.clock = FakeClock(start)
        self.cache = StatusCache()
        self.session = self.pi.session()
        self.client = BoilerClient(self.pi.config, session=self.session, dry_run=dry_run)
        self.poller = StatusPoller(self.client, self.cache, self.clock, self.pi.config)
        self.commands = ManualCommands(self.client, self.cache, refresh=self.poller.poll_once)
        if poll_first:
            self.poller.poll_once()

    def posts(self) -> list[tuple[str, str]]:
        return [call for call in self.pi.calls if call[0] == "POST"]

    def sent(self) -> list[str]:
        return [name for name, _ in self.pi.commands]


@pytest.fixture
def rig() -> Iterator[Rig]:
    made = Rig()
    yield made
    made.session.close()


def on(rig: Rig) -> CommandResult:
    return rig.commands.manual_on(source="ui", who=SENTINEL_WHO)


# --- manual On / Off / Auto: one POST each, the boiler Pi builds the file ----------------------


def test_manual_on_posts_once_and_confirms_the_mode(rig: Rig) -> None:
    result = on(rig)
    assert result.ok and result.sent
    assert rig.sent() == ["manual_on"]
    assert rig.posts() == [("POST", "/manualon/")]  # no file, no second request
    assert result.observed_mode == "Manual"
    assert "for about 3 hours" in result.message and "now reports Manual" in result.message


def test_manual_off_posts_once(rig: Rig) -> None:
    result = rig.commands.manual_off(source="mqtt", who="ha")
    assert result.ok and result.sent
    assert rig.posts() == [("POST", "/manualoff/")]
    assert result.observed_mode == "Manual"
    assert "about an hour" in result.message


def test_auto_returns_to_the_schedule(rig: Rig) -> None:
    on(rig)
    result = rig.commands.auto(source="email", who="owner@example.com")
    assert result.ok and result.sent
    assert rig.sent() == ["manual_on", "auto"]
    assert result.observed_mode == "Auto"


def test_the_commands_do_not_need_a_status() -> None:
    cold = Rig(poll_first=False)  # the boiler Pi decides the level, not the Orchestrator
    assert cold.commands.manual_on(source="ui", who="x").ok
    assert cold.commands.manual_off(source="ui", who="x").ok
    assert cold.commands.auto(source="ui", who="x").ok
    cold.session.close()


def test_the_orchestrator_never_sends_a_level_or_a_time(rig: Rig) -> None:
    on(rig)
    assert rig.pi.commands == [("manual_on", None)]  # no body at all


# --- a duration cannot be chosen --------------------------------------------------------------


@pytest.mark.parametrize("hours", [0.5, 2, 12])
def test_a_chosen_duration_is_refused_and_nothing_is_sent(rig: Rig, hours: float) -> None:
    for command in (rig.commands.manual_on, rig.commands.manual_off):
        result = command(source="mqtt", who="ha", hours=hours)
        assert not result.ok and not result.sent
        assert "duration cannot be chosen" in result.message
    assert rig.posts() == []


# --- failures: exactly one attempt, no retry --------------------------------------------------


def test_a_rejected_command_is_reported_and_not_retried(rig: Rig) -> None:
    rig.pi.reject_uploads_with = 503
    result = on(rig)
    assert not result.ok and not result.sent
    assert "not sent" in result.message and "503" in result.message
    assert len(rig.posts()) == 1


def test_an_unreachable_boiler_pi_is_reported_and_not_retried(rig: Rig) -> None:
    rig.pi.offline = True
    result = on(rig)
    assert not result.ok and "not sent" in result.message
    assert len(rig.posts()) == 1


# --- dry run ----------------------------------------------------------------------------------


def test_in_dry_run_nothing_is_posted() -> None:
    dry = Rig(dry_run=True)
    result = on(dry)
    assert result.ok and not result.sent
    assert "dry run" in result.message
    assert dry.posts() == [] and dry.pi.commands == []
    dry.session.close()


# --- relay runs -------------------------------------------------------------------------------


def test_relay_on_sends_the_json_line_the_boiler_service_reads(rig: Rig) -> None:
    result = rig.commands.relay_on(2, 150, source="email", who="owner@example.com")
    assert result.ok and result.sent
    assert rig.pi.commands == [("relay_on", {"swith": 2, "duration": 150})]
    assert rig.posts() == [("POST", "/relayon/")]
    assert "about 90 s" in result.message


@pytest.mark.parametrize(
    ("relay", "seconds"), [(0, 600), (1, 600), (8, 600), (4, 10), (4, 1), (4, 90000)]
)
def test_relay_requests_outside_the_rules_are_refused(rig: Rig, relay: int, seconds: int) -> None:
    result = rig.commands.relay_on(relay, seconds, source="mqtt", who="ha")
    assert not result.ok and not result.sent
    assert rig.posts() == []


def test_relay_off_posts_to_relayoff_without_a_time_limit_by_default(rig: Rig) -> None:
    result = rig.commands.relay_off(5, source="ui", who="x")
    assert result.ok and result.sent
    assert rig.pi.commands == [("relay_off", {"swith": 5, "duration": 0})]
    assert rig.posts() == [("POST", "/relayoff/")]
    assert "without a time limit" in result.message


def test_relay_off_for_a_while_comes_back_on(rig: Rig) -> None:
    result = rig.commands.relay_off(5, 300, source="mqtt", who="ha")
    assert result.ok and "off for 300 s, then back on" in result.message
    assert rig.pi.commands == [("relay_off", {"swith": 5, "duration": 300})]


@pytest.mark.parametrize("seconds", [0, -1, -3600])
def test_a_relay_duration_of_zero_or_less_means_on_without_a_time_limit(
    rig: Rig, seconds: int
) -> None:
    result = rig.commands.relay_on(3, seconds, source="ui", who="x")
    assert result.ok and "without a time limit" in result.message
    assert rig.pi.commands == [("relay_on", {"swith": 3, "duration": 0})]


def test_relays_0_and_1_can_never_be_switched_off_manually_either(rig: Rig) -> None:
    for relay in (0, 1):
        result = rig.commands.relay_off(relay, source="ui", who="x")
        assert not result.ok and "belong to the boiler" in result.message
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
    assert "observed_mode=Manual" in audit_lines[0]
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
