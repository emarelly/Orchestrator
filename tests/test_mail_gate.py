"""Sender validation, command recognition and security alerts (FR-LEG-3b)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orchestrator.mail_client import GoogleMailClient, MailMessage, parse_message
from orchestrator.mail_gate import (
    ALERT_BODY_CHARS,
    AlertLimiter,
    AlertOutcome,
    Allowance,
    CommandName,
    Decision,
    ParsedCommand,
    SecurityAlerts,
    build_alert,
    classify_message,
    parse_command,
)
from tests.fake_gmail import FakeGmailService, gmail_message
from tests.fake_google import FakeHttpError
from tests.fakes import FakeClock

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=TZ)
OWN = "orchestrator@example.com"
USERS = ["eli@gmail.com", "Dana@Example.org "]
ALERT_TO = "emarelly@gmail.com"


def mail(
    sender: str = "eli@gmail.com",
    subject: str = "on",
    *,
    body: str = "hello",
    authenticated: str | None = "mx.google.com; spf=pass",
    extra: list[tuple[str, str]] | None = None,
    name: str = "",
) -> MailMessage:
    shown = f"{name} <{sender}>" if name else sender
    return parse_message(
        gmail_message("m1", shown, subject, body, authenticated=authenticated, extra_headers=extra)
    )


# --- commands by the first word ---------------------------------------------------------------


@pytest.mark.parametrize("word", ["status", "log", "on", "off", "auto", "reset"])
def test_each_command_is_recognised_by_its_first_word_in_any_case(word: str) -> None:
    for subject in (word, word.upper(), f"  {word.capitalize()}  "):
        parsed = parse_command(subject)
        assert parsed is not None and parsed.command is not None
        assert parsed.command.name is CommandName(word)


def command_of(subject: str) -> ParsedCommand:
    parsed = parse_command(subject)
    assert parsed is not None and parsed.command is not None, subject
    return parsed.command


def test_only_commands_that_change_something_need_confirmation() -> None:
    words = ("status", "log", "on", "off", "auto", "reset")
    assert {word: command_of(word).changes_state for word in words} == {
        "status": False,
        "log": False,
        "on": True,
        "off": True,
        "auto": True,
        "reset": True,
    }


@pytest.mark.parametrize(
    "subject",
    [
        "",
        "   ",
        "Re: on",
        "please turn on",  # a substring is not a command
        "online",
        "once",
        "statuses",
        "Xqzvbnmasd",  # a confirmation token
        "fwd: switch 4 600",
        "0n",
    ],
)
def test_anything_else_is_not_a_command(subject: str) -> None:
    assert parse_command(subject) is None


def test_switch_takes_a_relay_and_a_duration() -> None:
    parsed = parse_command("Switch 4 600")
    assert parsed is not None and parsed.command is not None
    assert (parsed.command.relay, parsed.command.seconds) == (4, 600)
    assert parsed.command.changes_state


@pytest.mark.parametrize(
    ("subject", "fragment"),
    [
        ("switch", "use: switch"),
        ("switch 4", "use: switch"),
        ("switch 4 600 now", "use: switch"),
        ("switch four 600", "use: switch"),
        ("switch 4 ten", "use: switch"),
        ("switch -4 600", "use: switch"),
        ("switch ٤ 600", "use: switch"),  # non-ASCII digits
        ("switch 4 99999999999", "use: switch"),
        ("switch 1 600", "cannot be switched manually"),
        ("switch 0 600", "cannot be switched manually"),
        ("switch 8 600", "cannot be switched manually"),
        ("switch 4 10", "more than 10"),
        ("switch 4 86401", "at most"),
    ],
)
def test_a_bad_switch_is_explained_and_never_becomes_a_command(subject: str, fragment: str) -> None:
    parsed = parse_command(subject)
    assert parsed is not None and parsed.command is None
    assert parsed.error is not None and fragment in parsed.error


@pytest.mark.parametrize("subject", ["on now", "status please", "auto 1", "reset 5"])
def test_commands_without_arguments_refuse_extra_words(subject: str) -> None:
    parsed = parse_command(subject)
    assert parsed is not None and parsed.command is None
    assert parsed.error is not None and "takes no arguments" in parsed.error


# --- who may command --------------------------------------------------------------------------


def test_an_authorized_authenticated_sender_is_accepted_whatever_the_case() -> None:
    assert classify_message(mail("eli@gmail.com"), USERS, OWN).decision is Decision.ACCEPT
    assert classify_message(mail("DANA@example.ORG"), USERS, OWN).decision is Decision.ACCEPT


def test_the_display_name_cannot_fool_the_check() -> None:
    forged = mail("evil@example.net", name="eli@gmail.com")
    verdict = classify_message(forged, USERS, OWN)
    assert verdict.decision is Decision.REJECT


def test_an_unlisted_sender_is_rejected() -> None:
    verdict = classify_message(mail("stranger@example.net"), USERS, OWN)
    assert verdict.decision is Decision.REJECT
    assert "not in the authorized list" in verdict.reason


@pytest.mark.parametrize(
    "header",
    ["", "eli@gmail.com, evil@example.net", "no address", "Eli <eli@gmail.com>, <e@x.org>"],
)
def test_a_sender_that_is_not_one_address_is_rejected(header: str) -> None:
    message = parse_message(gmail_message("m1", header, "on"))
    assert message.sender == ""
    assert classify_message(message, USERS, OWN).decision is Decision.REJECT


def test_an_empty_authorized_list_accepts_nobody() -> None:
    assert classify_message(mail("eli@gmail.com"), [], OWN).decision is Decision.REJECT


def test_an_authorized_sender_whose_mail_failed_authentication_is_rejected() -> None:
    forged = mail("eli@gmail.com", authenticated="mx.google.com; spf=fail; dkim=fail")
    verdict = classify_message(forged, USERS, OWN)
    assert verdict.decision is Decision.REJECT
    assert "SPF/DKIM" in verdict.reason


def test_a_missing_authentication_header_is_accepted_as_the_requirement_says() -> None:
    assert classify_message(mail(authenticated=None), USERS, OWN).decision is Decision.ACCEPT


def test_our_own_mail_and_automatic_mail_are_ignored_without_an_alert() -> None:
    assert classify_message(mail(OWN), USERS, OWN).decision is Decision.IGNORE
    bounce = mail("mailer-daemon@googlemail.com")
    assert classify_message(bounce, USERS, OWN).decision is Decision.IGNORE
    auto = mail("stranger@example.net", extra=[("Auto-Submitted", "auto-replied")])
    assert classify_message(auto, USERS, OWN).decision is Decision.IGNORE
    auto_from_user = mail("eli@gmail.com", extra=[("Precedence", "bulk")])
    assert classify_message(auto_from_user, USERS, OWN).decision is Decision.IGNORE


def test_a_forged_own_address_is_not_ignored() -> None:
    forged = mail(OWN, authenticated="mx.google.com; spf=fail")
    verdict = classify_message(forged, USERS, OWN)
    assert verdict.decision is Decision.REJECT
    assert "own address" in verdict.reason


# --- the rate limit ------------------------------------------------------------------------


def test_one_alert_per_sender_per_hour_and_the_held_back_mails_are_counted() -> None:
    clock = FakeClock(NOW)
    limiter = AlertLimiter(clock)
    assert limiter.decide("a@x.org").send is True
    for _ in range(3):
        clock.advance(timedelta(minutes=5))
        assert limiter.decide("a@x.org").send is False
    clock.advance(timedelta(minutes=50))
    allowance = limiter.decide("a@x.org")
    assert allowance.send is True and allowance.suppressed == 3
    clock.advance(timedelta(hours=2))
    assert limiter.decide("a@x.org") == Allowance(True, 0)


def test_other_senders_have_their_own_hour() -> None:
    limiter = AlertLimiter(FakeClock(NOW))
    assert limiter.decide("a@x.org").send
    assert limiter.decide("b@x.org").send
    assert not limiter.decide("a@x.org").send


def test_a_daily_maximum_protects_the_inbox_and_resets_the_next_day() -> None:
    clock = FakeClock(NOW)
    limiter = AlertLimiter(clock, daily_max=3)
    sent = [limiter.decide(f"s{n}@x.org").send for n in range(6)]
    assert sent == [True, True, True, False, False, False]
    clock.advance(timedelta(days=1))
    allowance = limiter.decide("s5@x.org")
    assert allowance.send is True and allowance.suppressed == 1


def test_the_hour_is_measured_in_real_time_across_a_clock_change() -> None:
    # 2026-10-25 01:30 +03:00 happens twice: the clocks go back at 02:00 summer time
    first = datetime(2026, 10, 25, 1, 30, tzinfo=TZ, fold=0)
    clock = FakeClock(first)
    limiter = AlertLimiter(clock)
    assert limiter.decide("a@x.org").send
    clock.advance(timedelta(minutes=30))  # still inside the hour
    assert not limiter.decide("a@x.org").send
    clock.advance(timedelta(minutes=31))
    assert limiter.decide("a@x.org").send


def test_the_memory_of_senders_is_bounded() -> None:
    clock = FakeClock(NOW)
    limiter = AlertLimiter(clock, daily_max=10_000)
    for n in range(600):
        clock.advance(timedelta(minutes=1))
        limiter.decide(f"s{n}@x.org")
    assert limiter.tracked <= 200  # senders whose hour is over are forgotten


# --- the alert -------------------------------------------------------------------------------


def rig() -> tuple[SecurityAlerts, FakeGmailService, FakeClock]:
    service = FakeGmailService(OWN)
    clock = FakeClock(NOW)
    return SecurityAlerts(GoogleMailClient(service), ALERT_TO, clock), service, clock


def test_a_rejected_mail_produces_one_alert_with_the_required_facts() -> None:
    alerts, service, _ = rig()
    message = mail(
        "stranger@example.net",
        "on",
        body="please heat the water " + "x" * 2000,
        name="Mr Stranger",
        authenticated="mx.google.com; spf=fail",
    )
    assert alerts.rejected(message, "the sender is not in the authorized list") is AlertOutcome.SENT
    assert len(service.sent) == 1
    sent = service.sent[0]
    text = sent.get_content()
    assert sent["To"] == ALERT_TO and sent["From"] == OWN
    assert "stranger@example.net" in sent["Subject"]
    for fact in (
        "Reason: the sender is not in the authorized list",
        "Received: 2026-10-24T16:41:00+03:00",
        "display name: Mr Stranger",
        "To: orchestrator@example.com",
        "Subject: on",
        "Subject looks like a command: yes",
        "Authentication (SPF/DKIM): did not pass",
        "please heat the water",
    ):
        assert fact in text, fact
    body_part = text.split("---")[1].strip()
    assert len(body_part) == ALERT_BODY_CHARS


def test_the_alert_says_when_the_subject_is_not_a_command_and_when_authentication_is_unknown() -> (
    None
):
    alerts, service, _ = rig()
    alerts.rejected(mail("x@y.org", "hello there", authenticated=None), "r")
    text = service.sent[0].get_content()
    assert "Subject looks like a command: no" in text
    assert "not available" in text


def test_control_characters_and_long_values_in_an_attackers_mail_are_neutralised() -> None:
    message = mail("x@y.org", "bad\x00\x07subject", body="line\x1b[31mred\x00")
    alert = build_alert(message, "r", 0, NOW)
    assert "\x00" not in alert.body and "\x1b" not in alert.body and "\x07" not in alert.body


def test_a_second_mail_from_the_same_stranger_is_held_back_then_counted() -> None:
    alerts, service, clock = rig()
    stranger = mail("stranger@example.net")
    assert alerts.rejected(stranger, "r") is AlertOutcome.SENT
    assert alerts.rejected(stranger, "r") is AlertOutcome.HELD_BACK
    assert alerts.rejected(stranger, "r") is AlertOutcome.HELD_BACK
    clock.advance(timedelta(hours=1, minutes=1))
    assert alerts.rejected(stranger, "r") is AlertOutcome.SENT
    assert "held back since the last alert about this sender: 2" in service.sent[-1].get_content()
    assert len(service.sent) == 2


def test_nothing_is_sent_when_no_alert_address_is_configured() -> None:
    service = FakeGmailService(OWN)
    alerts = SecurityAlerts(GoogleMailClient(service), "  ", FakeClock(NOW))
    assert alerts.rejected(mail("x@y.org"), "r") is AlertOutcome.NOT_CONFIGURED
    assert service.sent == []


def test_a_failing_alert_is_logged_not_raised_and_is_tried_again_with_the_next_mail() -> None:
    alerts, service, _ = rig()
    client_ok = mail("stranger@example.net")
    alerts.rejected(client_ok, "r")  # first alert fine, learns the own address
    service.sent.clear()
    stranger2 = mail("other@example.net")
    service.error = FakeHttpError(503)
    assert alerts.rejected(stranger2, "r") is AlertOutcome.FAILED
    service.error = None
    assert alerts.rejected(stranger2, "r") is AlertOutcome.SENT  # not held back by the failure
    assert len(service.sent) == 1


def test_the_rejection_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    alerts, _, _ = rig()
    with caplog.at_level("WARNING"):
        alerts.rejected(mail("stranger@example.net"), "the sender is not in the authorized list")
    assert "event=mail_rejected" in caplog.text
    assert "stranger@example.net" in caplog.text


def test_the_alert_never_contains_the_authorized_list() -> None:
    alerts, service, _ = rig()
    alerts.rejected(mail("stranger@example.net"), "the sender is not in the authorized list")
    assert "eli@gmail.com" not in service.sent[0].get_content()
