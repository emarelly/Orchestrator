"""E-mail commands: two-factor confirmation, lockout, outcomes, the mail loop (FR-LEG-3/4)."""

import itertools
import re
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest

from orchestrator.mail_client import GoogleMailClient, MailError, MailMessage, parse_message
from orchestrator.mail_commands import (
    MailCommands,
    MailResult,
    ResultKind,
    new_token,
)
from orchestrator.mail_gate import SecurityAlerts
from orchestrator.mail_worker import MailWorker
from tests.fake_gmail import FakeGmailService, gmail_message
from tests.fake_google import FakeHttpError
from tests.test_manual_commands import Rig

OWN = "orchestrator@example.com"
ELI = "eli@gmail.com"
DANA = "dana@example.org"
ADMIN = "admin@example.com"
ALERT_TO = "emarelly@gmail.com"
TOKENS = ["abcdefghij", "klmnopqrst", "uvwxyzabcd", "efghijklmn", "opqrstuvwx", "yzabcdefgh"]


class Reboot:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def reboot(self) -> str:
        self.calls += 1
        if self.error:
            raise self.error
        return "reboot requested"


class Setup:
    def __init__(
        self,
        *,
        rebooter: Reboot | None = None,
        dry_run: bool = False,
        poll: bool = True,
        max_failures: int = 5,
        log_text: Callable[[], str] | None = None,
    ) -> None:
        self.rig = Rig(poll_first=poll, dry_run=dry_run)
        self.gmail = FakeGmailService(OWN)
        self.client = GoogleMailClient(self.gmail)
        self.alerts = SecurityAlerts(self.client, ALERT_TO, self.rig.clock)
        tokens = iter(TOKENS)
        self.commands = MailCommands(
            self.client,
            self.rig.commands,
            self.rig.cache,
            self.rig.clock,
            self.alerts,
            log_text=log_text or (lambda: "\n".join(f"log line {n}" for n in range(200))),
            rebooter=rebooter,
            admin_email=ADMIN,
            dry_run=dry_run,
            max_failures=max_failures,
            token_factory=lambda: next(tokens),
        )
        self.counter = itertools.count(1)

    def message(self, sender: str, subject: str, **kwargs: Any) -> MailMessage:
        raw = gmail_message(f"id{next(self.counter)}", sender, subject, "", **kwargs)
        return parse_message(raw)

    def send(self, sender: str, subject: str) -> MailResult:
        return self.commands.handle(self.message(sender, subject))

    def replies(self, to: str | None = None) -> list[Any]:
        return [m for m in self.gmail.sent if to is None or m["To"] == to]

    def last_reply(self) -> Any:
        return self.gmail.sent[-1]

    def boiler_posts(self) -> int:
        return len(self.rig.posts())


@pytest.fixture
def setup() -> Iterator[Setup]:
    made = Setup()
    yield made
    made.rig.session.close()


# --- answers that change nothing ------------------------------------------------------------


def test_status_is_answered_at_once_without_a_token(setup: Setup) -> None:
    result = setup.send(ELI, "status")
    assert result.kind is ResultKind.ANSWERED
    reply = setup.last_reply()
    assert reply["To"] == ELI and reply["Subject"] == "Re: status"
    text = reply.get_content()
    assert "Mode: Manual" in text or "Mode: Auto" in text
    assert "Connection to the boiler Pi: up" in text
    assert setup.boiler_posts() == 0


def test_log_returns_the_last_150_lines(setup: Setup) -> None:
    setup.send(ELI, "log")
    lines = setup.last_reply().get_content().splitlines()
    assert lines[0] == "log line 50" and lines[-1] == "log line 199" and len(lines) == 150


def test_a_log_that_cannot_be_read_is_reported_not_raised() -> None:
    def broken() -> str:
        raise OSError("down")

    made = Setup(log_text=broken)
    made.send(ELI, "log")
    assert "could not be read (OSError)" in made.last_reply().get_content()
    made.rig.session.close()


def test_status_says_so_when_nothing_is_known() -> None:
    made = Setup(poll=False)
    made.send(ELI, "status")
    assert "No reading yet" in made.last_reply().get_content()
    made.rig.session.close()


def test_unknown_text_and_bad_arguments_get_help_and_change_nothing(setup: Setup) -> None:
    assert setup.send(ELI, "hello there").kind is ResultKind.ANSWERED
    assert "Commands (first word" in setup.last_reply().get_content()
    setup.send(ELI, "switch 1 600")
    assert "cannot be switched manually" in setup.last_reply().get_content()
    setup.send(ELI, "on now")
    assert "takes no arguments" in setup.last_reply().get_content()
    assert setup.boiler_posts() == 0


# --- request and confirmation -------------------------------------------------------------


def test_a_changing_command_only_asks_for_the_token(setup: Setup) -> None:
    result = setup.send(ELI, "on")
    assert result.kind is ResultKind.REQUESTED
    reply = setup.last_reply()
    assert reply["To"] == ELI
    assert reply["Subject"] == TOKENS[0]  # the subject is the token itself
    text = reply.get_content()
    assert "Pending: manual ON" in text
    assert "Nothing has changed yet" in text and "120 seconds" in text
    assert "Mode:" in text  # the current status is part of the request mail
    assert setup.boiler_posts() == 0


def test_returning_the_token_runs_the_command_and_reports_the_real_outcome(setup: Setup) -> None:
    setup.send(ELI, "on")
    result = setup.send(ELI, TOKENS[0])
    assert result.kind is ResultKind.EXECUTED
    assert setup.boiler_posts() == 1
    done = setup.last_reply()
    assert done["To"] == ELI and done["Subject"].startswith("on: DONE")
    assert "DONE: Manual On" in done.get_content()
    assert "boiler Pi now reports" in done.get_content()


@pytest.mark.parametrize("form", ["Re: {t}", "RE:{t}", "  {t}  ", "{T}", "Aw: {t}"])
def test_a_reply_prefix_whitespace_and_case_are_tolerated(setup: Setup, form: str) -> None:
    setup.send(ELI, "off")
    token = TOKENS[0]
    assert setup.send(ELI, form.format(t=token, T=token.upper())).kind is ResultKind.EXECUTED


def test_a_token_works_only_once(setup: Setup) -> None:
    setup.send(ELI, "on")
    setup.send(ELI, TOKENS[0])
    again = setup.send(ELI, TOKENS[0])
    assert again.kind is ResultKind.FAILED_CONFIRMATION
    assert setup.boiler_posts() == 1


def test_a_wrong_token_does_nothing_and_the_right_one_still_works(setup: Setup) -> None:
    setup.send(ELI, "on")
    wrong = setup.send(ELI, "zzzzzzzzzz")
    assert wrong.kind is ResultKind.FAILED_CONFIRMATION and "wrong" in wrong.detail
    assert setup.boiler_posts() == 0
    assert setup.send(ELI, TOKENS[0]).kind is ResultKind.EXECUTED


def test_the_token_must_come_from_the_same_sender(setup: Setup) -> None:
    setup.send(ELI, "on")
    other = setup.send(DANA, TOKENS[0])
    assert other.kind is ResultKind.FAILED_CONFIRMATION and "different sender" in other.detail
    assert setup.boiler_posts() == 0
    assert setup.send(ELI, TOKENS[0]).kind is ResultKind.EXECUTED


def test_a_token_expires_after_the_window_but_not_before(setup: Setup) -> None:
    setup.send(ELI, "on")
    setup.rig.clock.advance(timedelta(seconds=120))
    assert setup.send(ELI, TOKENS[0]).kind is ResultKind.EXECUTED
    setup.send(ELI, "off")
    setup.rig.clock.advance(timedelta(seconds=121))
    late = setup.send(ELI, TOKENS[1])
    assert late.kind is ResultKind.FAILED_CONFIRMATION and "expired" in late.detail
    assert setup.boiler_posts() == 1
    assert setup.send(ELI, TOKENS[1]).kind is ResultKind.FAILED_CONFIRMATION  # nothing pending now


def test_only_the_latest_request_is_pending(setup: Setup) -> None:
    setup.send(ELI, "on")
    setup.send(ELI, "off")
    assert setup.send(ELI, TOKENS[0]).kind is ResultKind.FAILED_CONFIRMATION
    assert setup.send(ELI, TOKENS[1]).kind is ResultKind.EXECUTED
    assert [name for name, _ in setup.rig.pi.commands] == ["manual_off"]


def test_a_token_without_any_request_is_a_failed_confirmation(setup: Setup) -> None:
    result = setup.send(ELI, "qwertyuiop")
    assert result.kind is ResultKind.FAILED_CONFIRMATION
    assert "no pending command" in result.detail


def test_a_failed_confirmation_is_reported_to_the_alert_address(setup: Setup) -> None:
    setup.send(ELI, "on")
    setup.send(ELI, "zzzzzzzzzz")
    alert = setup.replies(ALERT_TO)[0]
    assert "confirmation token rejected" in alert.get_content()
    setup.send(ELI, "yyyyyyyyyy")  # rate limited: same sender within the hour
    assert len(setup.replies(ALERT_TO)) == 1


# --- lockout --------------------------------------------------------------------------------


def fail_times(setup: Setup, times: int) -> MailResult:
    result = MailResult(ResultKind.ANSWERED)
    for _ in range(times):
        result = setup.send(ELI, "zzzzzzzzzz")
    return result


def test_five_failures_lock_e_mail_commands_and_tell_the_admin(setup: Setup) -> None:
    setup.send(ELI, "on")
    assert fail_times(setup, 5).kind is ResultKind.LOCKED_OUT
    assert any("locked" in m["Subject"].lower() for m in setup.replies(ADMIN))
    assert "blocked for 15 minutes" in setup.replies(ELI)[-1].get_content()
    blocked = setup.send(ELI, "status")  # even harmless commands wait
    assert blocked.kind is ResultKind.LOCKED_OUT
    assert setup.send(ELI, TOKENS[0]).kind is ResultKind.LOCKED_OUT
    assert setup.boiler_posts() == 0


def test_the_lockout_ends_after_its_period_and_the_pending_command_is_gone(setup: Setup) -> None:
    setup.send(ELI, "on")
    fail_times(setup, 5)
    setup.rig.clock.advance(timedelta(minutes=15, seconds=1))
    assert setup.send(ELI, TOKENS[0]).kind is ResultKind.FAILED_CONFIRMATION
    assert setup.send(ELI, "status").kind is ResultKind.ANSWERED
    assert setup.boiler_posts() == 0


def test_a_correct_confirmation_resets_the_failure_count(setup: Setup) -> None:
    setup.send(ELI, "on")
    fail_times(setup, 4)
    setup.send(ELI, TOKENS[0])
    setup.send(ELI, "off")
    assert (
        fail_times(setup, 4).kind is ResultKind.FAILED_CONFIRMATION
    )  # not locked: count restarted
    assert setup.send(ELI, TOKENS[1]).kind is ResultKind.EXECUTED


# --- the real outcome -----------------------------------------------------------------------


def test_the_done_mail_reports_a_failure_when_the_command_could_not_run() -> None:
    made = Setup()
    made.rig.pi.reject_uploads_with = 503  # the boiler Pi refuses the command
    made.send(ELI, "on")
    made.send(ELI, TOKENS[0])
    done = made.last_reply()
    assert done["Subject"].startswith("on: FAILED")
    assert "not sent" in done.get_content() and "503" in done.get_content()
    assert made.boiler_posts() == 1
    made.rig.session.close()


def test_a_dry_run_is_not_reported_as_a_success_on_the_boiler() -> None:
    made = Setup(dry_run=True)
    made.send(ELI, "off")
    made.send(ELI, TOKENS[0])
    assert "dry run" in made.last_reply().get_content().lower()
    made.rig.session.close()


def test_auto_and_off_run_after_confirmation(setup: Setup) -> None:
    setup.send(ELI, "off")
    setup.send(ELI, TOKENS[0])
    setup.send(ELI, "auto")
    setup.send(ELI, TOKENS[1])
    assert [name for name, _ in setup.rig.pi.commands] == ["manual_off", "auto"]
    assert setup.rig.pi.status["Status"] == "Auto"


def test_switch_runs_a_relay_after_confirmation(setup: Setup) -> None:
    setup.send(ELI, "switch 4 600")
    assert "switch relay 4 on for 600 seconds" in setup.last_reply().get_content()
    setup.send(ELI, TOKENS[0])
    done = setup.last_reply().get_content()
    assert "DONE" in done and "Relay 4 on for 600 s" in done
    assert setup.boiler_posts() == 1


def test_reset_needs_a_configured_rebooter() -> None:
    made = Setup()
    made.send(ELI, "reset")
    made.send(ELI, TOKENS[0])
    assert "not configured" in made.last_reply().get_content()
    made.rig.session.close()


def test_reset_reboots_only_after_confirmation_and_reports_failures_without_details() -> None:
    reboot = Reboot()
    made = Setup(rebooter=reboot)
    made.send(ELI, "reset")
    assert reboot.calls == 0
    made.send(ELI, TOKENS[0])
    assert reboot.calls == 1 and "DONE: reboot requested" in made.last_reply().get_content()
    failing = Reboot(RuntimeError("password hunter2 rejected"))
    other = Setup(rebooter=failing)
    other.send(ELI, "reset")
    other.send(ELI, TOKENS[0])
    text = other.last_reply().get_content()
    assert "FAILED" in text and "RuntimeError" in text and "hunter2" not in text
    for item in (made, other):
        item.rig.session.close()


def test_reset_is_not_run_in_a_dry_run() -> None:
    reboot = Reboot()
    made = Setup(rebooter=reboot, dry_run=True)
    made.send(ELI, "reset")
    made.send(ELI, TOKENS[0])
    assert reboot.calls == 0 and "dry run" in made.last_reply().get_content()
    made.rig.session.close()


def test_a_reply_that_cannot_be_sent_is_logged_and_does_not_stop_the_command(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    setup.send(ELI, "on")
    setup.client.own_address()
    setup.gmail.error = FakeHttpError(503)
    with caplog.at_level("ERROR"):
        result = setup.send(ELI, TOKENS[0])
    assert result.kind is ResultKind.EXECUTED
    assert "event=mail_reply_failed" in caplog.text


def test_commands_are_audited_with_the_sender(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        setup.send(ELI, "on")
        setup.send(ELI, TOKENS[0])
    assert "stage=requested" in caplog.text and "stage=confirmed" in caplog.text
    assert "stage=done" in caplog.text and f"who=mail:{ELI}" in caplog.text


# --- tokens ---------------------------------------------------------------------------------


def test_tokens_are_ten_lower_case_letters_and_do_not_repeat() -> None:
    tokens = {new_token() for _ in range(2000)}
    assert all(re.fullmatch(r"[a-z]{10}", t) for t in tokens)
    assert len(tokens) == 2000


# --- the mail loop ----------------------------------------------------------------------------


class World(Setup):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.worker = MailWorker(
            self.client, self.commands, self.alerts, [ELI, DANA], interval_s=0.05
        )

    def deliver(self, sender: str, subject: str, **kwargs: Any) -> str:
        raw = gmail_message(f"w{next(self.counter)}", sender, subject, "", **kwargs)
        self.gmail.add(raw)
        return str(raw["id"])


@pytest.fixture
def world() -> Iterator[World]:
    made = World()
    yield made
    made.rig.session.close()


def test_the_loop_answers_commands_and_empties_the_inbox(world: World) -> None:
    world.deliver(ELI, "status")
    cycle = world.worker.process_inbox()
    assert (cycle.accepted, cycle.ignored, cycle.rejected, cycle.errors) == (1, 0, 0, 0)
    assert world.gmail.inbox == {} and len(world.gmail.trash) == 1
    assert world.last_reply()["To"] == ELI


def test_the_whole_two_step_exchange_through_the_inbox(world: World) -> None:
    world.deliver(ELI, "on")
    world.worker.process_inbox()
    token = world.last_reply()["Subject"]
    world.deliver(ELI, f"Re: {token}")
    world.worker.process_inbox()
    assert world.boiler_posts() == 1
    assert world.gmail.inbox == {}


def test_a_stranger_is_alerted_about_removed_and_never_answered(world: World) -> None:
    world.deliver("stranger@example.net", "on")
    cycle = world.worker.process_inbox()
    assert cycle.rejected == 1
    assert world.gmail.inbox == {}
    assert [m["To"] for m in world.gmail.sent] == [ALERT_TO]  # nothing went to the stranger
    assert world.boiler_posts() == 0


def test_a_forged_sender_whose_authentication_failed_does_not_command_the_boiler(
    world: World,
) -> None:
    world.deliver(ELI, "on", authenticated="mx.google.com; spf=fail; dkim=fail")
    cycle = world.worker.process_inbox()
    assert cycle.rejected == 1 and cycle.accepted == 0
    assert [m["To"] for m in world.gmail.sent] == [ALERT_TO]


def test_own_and_automatic_mail_is_removed_silently(world: World) -> None:
    world.deliver(OWN, "Zzzzzzzzzz")
    world.deliver(ELI, "out of office", extra_headers=[("Auto-Submitted", "auto-replied")])
    cycle = world.worker.process_inbox()
    assert cycle.ignored == 2
    assert world.gmail.sent == [] and world.gmail.inbox == {}


def test_one_failing_message_does_not_block_the_next(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = world.deliver(ELI, "status")
    world.deliver(DANA, "status")
    original = world.commands.handle

    def flaky(message: MailMessage) -> MailResult:
        if message.message_id == first:
            raise RuntimeError("bug")
        return original(message)

    monkeypatch.setattr(world.commands, "handle", flaky)
    cycle = world.worker.process_inbox()
    assert cycle.errors == 1 and cycle.accepted == 1
    assert world.gmail.inbox == {}  # the failing one is removed too, so it cannot repeat forever


def test_the_oldest_mail_is_handled_first(world: World) -> None:
    for number, (day, subject) in enumerate([(24, "off"), (23, "on")]):
        raw = gmail_message(f"o{number}", ELI, subject, "")
        for header in raw["payload"]["headers"]:
            if header["name"] == "Date":
                header["value"] = f"Sat, {day} Oct 2026 10:00:00 +0300"
        world.gmail.add(raw)
    world.worker.process_inbox()
    pending = [m["Subject"] for m in world.gmail.sent]
    assert pending == [TOKENS[0], TOKENS[1]]
    assert "manual ON" in world.gmail.sent[0].get_content()  # "on" (23rd) came before "off" (24th)


def test_a_large_inbox_is_worked_off_in_batches(world: World) -> None:
    for _ in range(25):
        world.deliver(ELI, "status")
    first = world.worker.process_inbox()
    assert first.more is True and first.accepted == 20
    second = world.worker.process_inbox()
    assert second.more is False and second.accepted == 5


def test_lost_authorization_is_flagged_and_cleared_when_it_returns(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.gmail.error = FakeHttpError(401)
    with caplog.at_level("INFO"):
        assert world.worker.process_inbox().errors == 1
        world.worker.process_inbox()
    assert world.worker.auth_lost is True
    assert caplog.text.count("event=mail_auth_lost") == 1
    world.gmail.error = None
    world.worker.process_inbox()
    assert world.worker.auth_lost is False


def test_a_network_problem_ends_the_pass_quietly(world: World) -> None:
    world.gmail.error = TimeoutError("slow")
    cycle = world.worker.process_inbox()
    assert cycle.errors == 1 and world.worker.auth_lost is False


def test_a_message_that_cannot_be_removed_is_logged(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world.deliver(ELI, "status")

    def refuse(_: str) -> None:
        raise MailError("nope")

    monkeypatch.setattr(world.client, "remove", refuse)
    with caplog.at_level("ERROR"):
        world.worker.process_inbox()
    assert "event=mail_remove_failed" in caplog.text


def wait_until(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_the_thread_checks_the_inbox_repeatedly_and_stops_on_request(world: World) -> None:
    stop = threading.Event()
    thread = world.worker.start(stop)
    world.deliver(ELI, "status")
    assert wait_until(lambda: world.gmail.inbox == {} and bool(world.gmail.sent))
    world.deliver(DANA, "status")
    assert wait_until(lambda: len(world.gmail.sent) == 2)
    assert world.worker.last_cycle is not None
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
