"""Gmail client: reading commands, sender and authentication facts, sending (FR-LEG-3b/4)."""

import base64
from typing import Any

import pytest

from orchestrator.mail_client import (
    MAX_BODY_CHARS,
    GoogleMailClient,
    MailAuthError,
    MailError,
    MailUnavailable,
    OutgoingMail,
    authentication_result,
    build_raw_message,
    is_automatic,
    parse_message,
    parse_sender,
)
from tests.fake_gmail import FakeGmailService, b64, gmail_message
from tests.fake_google import FakeHttpError


def make() -> tuple[GoogleMailClient, FakeGmailService]:
    service = FakeGmailService()
    return GoogleMailClient(service), service


# --- sender ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "address", "name"),
    [
        ("Eli <Eli.M@Gmail.com>", "eli.m@gmail.com", "Eli"),
        ("  eli@gmail.com ", "eli@gmail.com", ""),
        ('"Doe, John" <john@example.org>', "john@example.org", "Doe, John"),
    ],
)
def test_the_sender_is_the_bare_lower_case_address(header: str, address: str, name: str) -> None:
    assert parse_sender(header) == (address, name)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "no address here",
        "a@example.com, b@example.com",  # two senders: never trusted as one
        "Name <not-an-address>",
        "a@b",
        "Eli <a@b.com>, <evil@x.org>",
    ],
)
def test_a_from_header_that_is_not_exactly_one_address_gives_no_sender(header: str) -> None:
    assert parse_sender(header)[0] == ""


# --- authentication and automatic mail ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mx.google.com; dkim=pass header.i=@x.com; spf=fail", True),
        ("mx.google.com; spf=pass smtp.mailfrom=x.com", True),
        ("mx.google.com; dkim=fail; spf=softfail", False),
        ("mx.google.com; dmarc=pass", False),  # dmarc alone is not what the requirement names
        ("evil.example.org; dkim=pass; spf=pass", False),  # not written by Gmail
    ],
)
def test_only_gmails_own_spf_or_dkim_pass_counts(value: str, expected: bool) -> None:
    assert authentication_result([("Authentication-Results", value)]) is expected


def test_no_authentication_header_means_unknown_not_failed() -> None:
    assert authentication_result([("Subject", "on")]) is None


def test_the_topmost_authentication_header_wins_over_one_added_by_the_sender() -> None:
    headers = [
        ("Authentication-Results", "mx.google.com; spf=fail; dkim=fail"),
        ("Authentication-Results", "mx.google.com; spf=pass"),  # a forged second one
    ]
    assert authentication_result(headers) is False


@pytest.mark.parametrize(
    ("headers", "sender", "expected"),
    [
        ([("Auto-Submitted", "auto-replied")], "a@b.com", True),
        ([("Auto-Submitted", "no")], "a@b.com", False),
        ([("Precedence", "bulk")], "a@b.com", True),
        ([("List-Id", "<news.example.com>")], "a@b.com", True),
        ([("X-Failed-Recipients", "x@y.com")], "a@b.com", True),
        ([], "mailer-daemon@googlemail.com", True),
        ([], "eli@gmail.com", False),
    ],
)
def test_bounces_and_bulk_mail_are_recognised_as_automatic(
    headers: list[tuple[str, str]], sender: str, expected: bool
) -> None:
    assert is_automatic(headers, sender) is expected


# --- reading a message ---------------------------------------------------------------------


def test_a_command_mail_is_read_into_plain_data() -> None:
    raw = gmail_message("m1", "Eli <Eli@Gmail.com>", " switch 4 600 ", "please\n")
    message = parse_message(raw)
    assert message.message_id == "m1"
    assert (message.sender, message.sender_name) == ("eli@gmail.com", "Eli")
    assert message.subject == "switch 4 600"
    assert message.body == "please"
    assert message.to == "orchestrator@example.com"
    assert message.authenticated is True
    assert message.automatic is False
    assert message.received is not None and message.received.utcoffset() is not None


def test_a_message_without_multipart_structure_is_read_too() -> None:
    message = parse_message(gmail_message("m2", "a@b.com", "on", "body", multipart=False))
    assert message.body == "body"


def test_the_plain_part_is_preferred_and_html_is_only_a_fallback_without_tags() -> None:
    both = parse_message(gmail_message("m3", "a@b.com", "s", "plain text", html="<b>html</b>"))
    assert both.body == "plain text"
    raw = gmail_message("m4", "a@b.com", "s", "x", html="<p>Hello <a href='http://x'>link</a></p>")
    raw["payload"]["parts"] = raw["payload"]["parts"][1:]  # html only
    only_html = parse_message(raw)
    assert "Hello" in only_html.body and "<" not in only_html.body


def test_the_body_charset_is_honoured_and_a_bad_one_does_not_crash() -> None:
    raw = gmail_message("m5", "a@b.com", "s", "")
    part = raw["payload"]["parts"][0]
    part["body"]["data"] = base64.urlsafe_b64encode("שלום".encode("iso-8859-8")).decode()
    part["headers"] = [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-8"}]
    assert parse_message(raw).body == "שלום"
    part["headers"] = [{"name": "Content-Type", "value": "text/plain; charset=no-such-charset"}]
    assert isinstance(parse_message(raw).body, str)


def test_a_huge_body_is_cut() -> None:
    raw = gmail_message("m6", "a@b.com", "s", "x" * (MAX_BODY_CHARS * 3))
    assert len(parse_message(raw).body) == MAX_BODY_CHARS


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"id": "x"},
        {"id": "x", "payload": {}},
        {"id": "x", "payload": {"headers": [{"name": "From"}, "junk", {"value": "v"}]}},
        {"id": "x", "payload": {"mimeType": "text/plain", "body": {"data": "!!not base64!!"}}},
    ],
)
def test_strange_messages_never_raise_and_have_no_sender(raw: dict[str, Any]) -> None:
    message = parse_message(raw)
    assert message.sender == ""


def test_a_bad_date_is_just_missing() -> None:
    raw = gmail_message("m7", "a@b.com", "s", "b")
    for header in raw["payload"]["headers"]:
        if header["name"] == "Date":
            header["value"] = "not a date"
    assert parse_message(raw).received is None


# --- the client: reading ---------------------------------------------------------------------


def test_the_inbox_is_listed_with_the_inbox_query_and_retries() -> None:
    client, service = make()
    service.add(gmail_message("a", "eli@gmail.com", "on"))
    service.add(gmail_message("b", "eli@gmail.com", "status"))
    read = client.list_inbox()
    assert [m.subject for m in read.messages] == ["on", "status"]
    assert read.more is False
    first = service.calls[0]
    assert first[0] == "list" and first[1]["q"] == "in:inbox"
    assert set(service.retries) == {2}


def test_a_large_inbox_is_read_in_pieces() -> None:
    client, service = make()
    for number in range(5):
        service.add(gmail_message(f"m{number}", "eli@gmail.com", "status"))
    read = client.list_inbox(limit=3)
    assert len(read.messages) == 3
    assert read.more is True


def test_an_empty_inbox_is_an_empty_read() -> None:
    client, _ = make()
    read = client.list_inbox()
    assert read.messages == () and read.more is False


def test_removing_moves_to_the_trash_and_a_second_removal_is_an_error() -> None:
    client, service = make()
    service.add(gmail_message("a", "eli@gmail.com", "on"))
    client.remove("a")
    assert service.trash == ["a"] and service.inbox == {}
    with pytest.raises(MailError):
        client.remove("a")


def test_the_account_address_comes_from_gmail_and_is_asked_once() -> None:
    client, service = make()
    assert client.own_address() == "orchestrator@example.com"
    assert client.own_address() == "orchestrator@example.com"
    assert [verb for verb, _ in service.calls].count("profile") == 1


def test_a_missing_account_address_is_an_error() -> None:
    client, service = make()
    service.address = "nonsense"
    with pytest.raises(MailError, match="account address"):
        client.own_address()


# --- the client: errors -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeHttpError(401), MailAuthError),
        (FakeHttpError(403, "forbidden"), MailAuthError),
        (FakeHttpError(403, "rateLimitExceeded"), MailUnavailable),
        (FakeHttpError(503), MailUnavailable),
        (TimeoutError("slow"), MailUnavailable),
        (ConnectionResetError("reset"), MailUnavailable),
        (FakeHttpError(404), MailError),
    ],
)
def test_google_and_network_errors_become_mail_errors(error: Exception, expected: type) -> None:
    client, service = make()
    service.error = error
    with pytest.raises(expected) as caught:
        client.list_inbox()
    assert type(caught.value) is expected


def test_a_programming_error_is_not_hidden() -> None:
    client, service = make()
    service.error = KeyError("bug")
    with pytest.raises(KeyError):
        client.list_inbox()


def test_error_text_never_contains_mail_content_or_response_body() -> None:
    client, service = make()
    error = FakeHttpError(401, "secret-reason")
    service.error = error
    with pytest.raises(MailAuthError) as caught:
        client.list_inbox()
    assert "secret-reason" not in str(caught.value)


# --- the client: sending ---------------------------------------------------------------------


def test_a_mail_is_sent_from_the_authorized_account_as_automatic_mail() -> None:
    client, service = make()
    client.send(OutgoingMail("eli@gmail.com", "Zxcvbnmasd", "Pending: on\nStatus: Auto"))
    assert len(service.sent) == 1
    sent = service.sent[0]
    assert sent["From"] == "orchestrator@example.com"  # not the placeholder of the legacy script
    assert sent["To"] == "eli@gmail.com"
    assert sent["Subject"] == "Zxcvbnmasd"
    assert sent["Auto-Submitted"] == "auto-generated"
    assert "Pending: on" in sent.get_content()


def test_non_ascii_text_survives_sending() -> None:
    client, service = make()
    client.send(OutgoingMail("eli@gmail.com", "שלום", "מצב: אוטומטי"))
    assert service.sent[0]["Subject"] == "שלום"
    assert service.sent[0].get_content().strip() == "מצב: אוטומטי"


def test_line_breaks_in_a_subject_cannot_inject_headers() -> None:
    raw = build_raw_message(
        "orchestrator@example.com",
        OutgoingMail("eli@gmail.com", "hello\r\nBcc: evil@x.org", "b"),
    )
    decoded = base64.urlsafe_b64decode(raw["raw"]).decode()
    assert "\nBcc:" not in decoded


@pytest.mark.parametrize(
    "recipient",
    ["", "no address", "a@b.com, c@d.com", "a@b.com\r\nBcc: x@y.org", "<a@b.com>", "a@b"],
)
def test_only_a_single_plain_recipient_is_accepted(recipient: str) -> None:
    with pytest.raises(MailError, match="recipient"):
        build_raw_message("orchestrator@example.com", OutgoingMail(recipient, "s", "b"))


def test_a_send_failure_is_reported_as_a_mail_error() -> None:
    client, service = make()
    client.own_address()
    service.error = FakeHttpError(503)
    with pytest.raises(MailUnavailable):
        client.send(OutgoingMail("eli@gmail.com", "s", "b"))


def test_b64_helper_round_trips_for_the_fake() -> None:
    assert base64.urlsafe_b64decode(b64("abc") + "==") == b"abc"
