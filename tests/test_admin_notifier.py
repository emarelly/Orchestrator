"""Notices to the admin: boiler down/up, calendar changes, Google authorization (FR-LEG-2)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orchestrator.admin_notifier import AdminNotifier
from orchestrator.calendar_sync import SyncReport
from orchestrator.mail_client import GoogleMailClient
from orchestrator.mail_worker import MailWorker
from orchestrator.reboot_grace import RebootGrace
from orchestrator.status_cache import ConnectionState, Transition
from tests.fake_gmail import FakeGmailService
from tests.fake_google import FakeHttpError
from tests.fakes import FakeClock
from tests.test_mail_commands import Setup as MailSetup
from tests.test_manual_commands import Rig

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=TZ)
ADMIN = "admin@example.com"
UP, DOWN, INACTIVE, UNKNOWN = (
    ConnectionState.UP,
    ConnectionState.DOWN,
    ConnectionState.INACTIVE,
    ConnectionState.UNKNOWN,
)


class Setup:
    def __init__(
        self, *, admin: str = ADMIN, with_grace: bool = False, daily_max: int = 30
    ) -> None:
        self.clock = FakeClock(NOW)
        self.gmail = FakeGmailService("orchestrator@example.com")
        self.grace = RebootGrace(self.clock) if with_grace else None
        self.notifier = AdminNotifier(
            GoogleMailClient(self.gmail),
            admin,
            self.clock,
            reboot_grace=self.grace,
            daily_max=daily_max,
        )

    def subjects(self) -> list[str]:
        return [m["Subject"] for m in self.gmail.sent]

    def last_text(self) -> str:
        return str(self.gmail.sent[-1].get_content())

    def transition(
        self, before: ConnectionState, after: ConnectionState, *, outage: timedelta | None = None
    ) -> Transition:
        error = "no answer" if after.unhealthy else None
        return Transition(before, after, self.clock.now().astimezone(UTC), outage, error)


@pytest.fixture
def setup() -> Setup:
    return Setup()


def report(
    *,
    ok: bool = True,
    diff: str = "",
    file_changed: bool = False,
    changed: bool = False,
    problems: tuple[str, ...] = (),
    error: str | None = None,
    auth_lost: bool = False,
) -> SyncReport:
    return SyncReport(
        at=NOW,
        ok=ok,
        schedules=None,
        boiler_changed=changed,
        timer_changed=False,
        diff=diff,
        problems=problems,
        error=error,
        auth_lost=auth_lost,
        file_changed=file_changed,
    )


# --- the boiler Pi ----------------------------------------------------------------------------


def test_a_down_boiler_is_announced_once_with_the_reason_and_the_time(setup: Setup) -> None:
    setup.notifier.on_transition(setup.transition(UP, DOWN))
    setup.notifier.on_transition(setup.transition(DOWN, DOWN))
    assert setup.subjects() == ["Orchestrator: Boiler problem: Pi not responding"]
    text = setup.last_text()
    assert "Last error: no answer" in text and "Since: 2026-10-24T16:41:00+03:00" in text
    assert "keep working" in text


def test_an_inactive_boiler_service_is_a_different_notice(setup: Setup) -> None:
    setup.notifier.on_transition(setup.transition(UP, INACTIVE))
    assert setup.subjects() == ["Orchestrator: Boiler problem: service not active"]


def test_a_change_between_two_unhealthy_states_is_not_a_new_problem(setup: Setup) -> None:
    setup.notifier.on_transition(setup.transition(UP, INACTIVE))
    setup.notifier.on_transition(setup.transition(INACTIVE, DOWN))
    assert len(setup.gmail.sent) == 1


def test_the_restored_boiler_is_announced_with_the_length_of_the_outage(setup: Setup) -> None:
    setup.notifier.on_transition(setup.transition(UP, DOWN))
    setup.notifier.on_transition(setup.transition(DOWN, UP, outage=timedelta(minutes=12)))
    assert setup.subjects()[-1] == "Orchestrator: Boiler Pi is back"
    assert "away for 12 minutes" in setup.last_text()


@pytest.mark.parametrize("before", [UNKNOWN, UP])
def test_starting_up_or_staying_up_is_not_news(setup: Setup, before: ConnectionState) -> None:
    setup.notifier.on_transition(setup.transition(before, UP))
    assert setup.gmail.sent == []


def test_a_requested_reboot_replaces_the_down_and_up_notices_by_one() -> None:
    made = Setup(with_grace=True)
    assert made.grace is not None
    made.grace.begin()
    made.clock.advance(timedelta(seconds=40))
    made.notifier.on_transition(made.transition(UP, DOWN))
    assert made.gmail.sent == []  # expected: the Pi is restarting
    made.clock.advance(timedelta(minutes=2))
    made.notifier.on_transition(made.transition(DOWN, UP, outage=timedelta(minutes=2)))
    assert made.subjects() == ["Orchestrator: Boiler Pi is back after the requested reboot"]
    assert "3 minutes after the reboot was requested" in made.last_text()  # 40 s + 2 min
    assert not made.grace.pending()


def test_a_boiler_that_stays_down_after_the_grace_time_is_reported() -> None:
    made = Setup(with_grace=True)
    assert made.grace is not None
    made.grace.begin()
    made.clock.advance(timedelta(minutes=10))  # the reboot should have been over long ago
    made.notifier.on_transition(made.transition(UP, DOWN))
    assert made.subjects() == ["Orchestrator: Boiler problem: Pi not responding"]
    made.notifier.on_transition(made.transition(DOWN, UP, outage=timedelta(minutes=30)))
    assert made.subjects()[-1] == "Orchestrator: Boiler Pi is back"  # not blamed on the reboot


def test_real_transitions_from_the_status_cache_produce_the_notices() -> None:
    rig = Rig()
    gmail = FakeGmailService("orchestrator@example.com")
    notifier = AdminNotifier(GoogleMailClient(gmail), ADMIN, rig.clock)
    status = rig.cache.snapshot().status
    assert status is not None
    now = rig.clock.now().astimezone(UTC)
    for number in range(3):
        transition = rig.cache.record_failure("timeout", now + timedelta(seconds=10 * number), 3)
        if transition:
            notifier.on_transition(transition)
    back = rig.cache.record_success(status, now + timedelta(minutes=5))
    assert back is not None
    notifier.on_transition(back)
    assert [m["Subject"] for m in gmail.sent] == [
        "Orchestrator: Boiler problem: Pi not responding",
        "Orchestrator: Boiler Pi is back",
    ]
    assert "away for 5 minutes" in gmail.sent[-1].get_content()
    rig.session.close()


# --- the calendar --------------------------------------------------------------------------


def test_a_changed_calendar_is_mailed_with_the_readable_difference(setup: Setup) -> None:
    diff = (
        "The following dates were added to the calendar:\r\n"
        "  boiler Monday 06:00:00 target 3.6 min 2"
    )
    setup.notifier.on_sync_report(report(diff=diff, file_changed=True, changed=True))
    assert setup.subjects() == ["Orchestrator: Boiler calendar changed"]
    assert "added to the calendar" in setup.last_text() and "\r" not in setup.last_text()


def test_skipped_entries_are_listed_after_the_difference(setup: Setup) -> None:
    problems = tuple(f"event {n} skipped" for n in range(25))
    setup.notifier.on_sync_report(
        report(diff="changed", file_changed=True, changed=True, problems=problems)
    )
    text = setup.last_text()
    assert "Entries that were skipped:" in text and "event 0 skipped" in text
    assert "event 24 skipped" not in text and "and 5 more" in text


def test_nothing_is_mailed_when_no_local_file_changed_for_example_after_a_restart(
    setup: Setup,
) -> None:
    setup.notifier.on_sync_report(report(diff="everything is new to this run", changed=True))
    setup.notifier.on_sync_report(report())
    assert setup.gmail.sent == []


def test_a_calendar_that_fails_three_times_in_a_row_is_reported_once(setup: Setup) -> None:
    for _ in range(2):
        setup.notifier.on_sync_report(report(ok=False, error="network problem"))
    assert setup.gmail.sent == []
    for _ in range(4):
        setup.notifier.on_sync_report(report(ok=False, error="network problem"))
    assert setup.subjects() == ["Orchestrator: Calendar cannot be read"]
    assert "network problem" in setup.last_text()


def test_a_calendar_that_works_again_is_announced_and_the_count_starts_over(setup: Setup) -> None:
    for _ in range(3):
        setup.notifier.on_sync_report(report(ok=False, error="x"))
    setup.notifier.on_sync_report(report())
    assert setup.subjects()[-1] == "Orchestrator: Calendar can be read again"
    for _ in range(2):
        setup.notifier.on_sync_report(report(ok=False, error="x"))
    setup.notifier.on_sync_report(report())  # a success in between resets the count
    for _ in range(2):
        setup.notifier.on_sync_report(report(ok=False, error="x"))
    assert len(setup.gmail.sent) == 2


def test_a_notice_that_could_not_be_sent_is_tried_again_at_the_next_failure(setup: Setup) -> None:
    setup.gmail.error = FakeHttpError(503)
    for _ in range(3):
        setup.notifier.on_sync_report(report(ok=False, error="x"))
    assert setup.gmail.sent == []
    setup.gmail.error = None
    setup.notifier.on_sync_report(report(ok=False, error="x"))
    assert setup.subjects() == ["Orchestrator: Calendar cannot be read"]


# --- Google authorization -------------------------------------------------------------------


def test_lost_authorization_is_announced_once_and_not_as_an_unreadable_calendar(
    setup: Setup,
) -> None:
    for _ in range(6):
        setup.notifier.on_sync_report(report(ok=False, auth_lost=True, error="refused"))
    assert setup.subjects() == ["Orchestrator: Google authorization needed"]
    assert "python -m orchestrator.auth" in setup.last_text()
    setup.notifier.on_sync_report(report())
    assert setup.subjects()[-1] == "Orchestrator: Google access works again"


def test_calendar_and_mail_losing_access_together_give_one_notice_each_way(setup: Setup) -> None:
    setup.notifier.on_sync_report(report(ok=False, auth_lost=True))
    setup.notifier.on_mail_auth(True)
    assert len(setup.gmail.sent) == 1
    setup.notifier.on_sync_report(report())  # the calendar is back, the mail is not
    assert len(setup.gmail.sent) == 1
    setup.notifier.on_mail_auth(False)
    assert setup.subjects()[-1] == "Orchestrator: Google access works again"
    assert len(setup.gmail.sent) == 2


def test_the_mail_check_reports_its_authorization_to_the_notifier() -> None:
    mail = MailSetup()
    seen: list[bool] = []
    worker = MailWorker(mail.client, mail.commands, mail.alerts, [], on_auth_change=seen.append)
    mail.gmail.error = FakeHttpError(401)
    worker.process_inbox()
    worker.process_inbox()
    mail.gmail.error = None
    worker.process_inbox()
    assert seen == [True, False]
    mail.rig.session.close()


def test_a_failing_listener_does_not_stop_the_mail_check() -> None:
    mail = MailSetup()

    def broken(lost: bool) -> None:
        raise RuntimeError("listener bug")

    worker = MailWorker(mail.client, mail.commands, mail.alerts, [], on_auth_change=broken)
    mail.gmail.error = FakeHttpError(401)
    assert worker.process_inbox().errors == 1
    assert worker.auth_lost is True
    mail.rig.session.close()


# --- limits and failures ---------------------------------------------------------------------


def test_nothing_is_sent_without_an_admin_address() -> None:
    made = Setup(admin="  ")
    made.notifier.on_transition(made.transition(UP, DOWN))
    made.notifier.on_sync_report(report(diff="d", file_changed=True, changed=True))
    assert made.gmail.sent == []


def test_a_notice_that_cannot_be_sent_is_logged_and_never_raises(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    setup.gmail.error = FakeHttpError(503)
    with caplog.at_level("ERROR"):
        setup.notifier.on_transition(setup.transition(UP, DOWN))
    assert "event=admin_notice_failed" in caplog.text


def test_a_daily_maximum_protects_the_inbox_and_says_so_once() -> None:
    made = Setup(daily_max=3)
    for _ in range(6):
        made.notifier.on_transition(made.transition(UP, DOWN))
        made.notifier.on_transition(made.transition(DOWN, UP))
    subjects = made.subjects()
    assert len(subjects) == 4
    assert subjects[-1] == "Orchestrator: Further notices held back today"
    made.clock.advance(timedelta(days=1))
    made.notifier.on_transition(made.transition(UP, DOWN))
    assert len(made.gmail.sent) == 5


def test_every_notice_carries_the_time_it_was_sent(setup: Setup) -> None:
    setup.notifier.on_transition(setup.transition(UP, DOWN))
    assert setup.last_text().rstrip().endswith("2026-10-24T16:41:00+03:00")
