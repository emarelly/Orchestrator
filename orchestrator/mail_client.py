"""Reading the command inbox and sending mail through Gmail (FR-LEG-2..4, FR-GOOG-1a).

``MailClient`` is the small interface the e-mail command code uses; ``GoogleMailClient`` is the
Gmail implementation over an injected API service (the real one is built by
:func:`build_mail_service`, imported only when needed). Nothing here decides who may command the
boiler: it only turns Gmail messages into plain data (:class:`MailMessage`) and back. Parsing never
raises on a strange message; it returns what it could read (an unreadable sender is the empty text)
so that the sender checks later treat it as an unauthorized message.

Removing a message from the inbox moves it to the trash (kept 30 days by Gmail), so a mistake can be
undone; the legacy script deleted it for good.
"""

import base64
import binascii
import importlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Protocol

from orchestrator.calendar_source import (
    CalendarAuthError,
    CalendarUnavailable,
    classify_error,
)
from orchestrator.logging_setup import kv

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 20_000
MAX_SEND_CHARS = 50_000
INBOX_QUERY = "in:inbox"
RETRIES = 2
_ADDRESS = re.compile(r"^[^@\s<>\",;]+@[^@\s<>\",;]+\.[^@\s<>\",;]+$")
_AUTH_RESULT = re.compile(r"\b(spf|dkim)=(\w+)", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]*>")
_ROBOT_SENDERS = ("mailer-daemon@", "postmaster@", "noreply@", "no-reply@")


class MailError(Exception):
    """Mail could not be read or sent; the text is safe to log (no secret, no mail content)."""


class MailAuthError(MailError):
    """Google refused the credentials: authorization is needed."""


class MailUnavailable(MailError):
    """A network or Google-side problem; try again later."""


@dataclass(frozen=True, slots=True)
class MailMessage:
    """One received message, reduced to what the command code needs."""

    message_id: str
    sender: str  # the bare address, lower case; empty if the From header is not one single address
    sender_name: str
    to: str
    subject: str
    body: str  # plain text only, links are never followed
    received: datetime | None
    automatic: bool  # bounce, bulk or auto-generated mail: never answered, never alerted about
    authenticated: bool | None  # SPF or DKIM pass in Gmail's own result; None if no such header


@dataclass(frozen=True, slots=True)
class OutgoingMail:
    to: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class InboxRead:
    messages: tuple[MailMessage, ...]
    more: bool  # the inbox holds more than was returned; the rest is read in the next cycle


class MailClient(Protocol):
    """What the e-mail command code needs from the mail system."""

    def own_address(self) -> str: ...

    def list_inbox(self, limit: int = 20) -> InboxRead: ...

    def remove(self, message_id: str) -> None: ...

    def send(self, mail: OutgoingMail) -> None: ...


# --- the Gmail API shape that is used --------------------------------------------------------


class _Request(Protocol):
    def execute(self, num_retries: int = 0) -> Any: ...


class _Messages(Protocol):
    def list(self, *, userId: str, q: str, maxResults: int) -> _Request: ...

    def get(self, *, userId: str, id: str, format: str) -> _Request: ...

    def trash(self, *, userId: str, id: str) -> _Request: ...

    def send(self, *, userId: str, body: dict[str, str]) -> _Request: ...


class _Users(Protocol):
    def messages(self) -> _Messages: ...

    def getProfile(self, *, userId: str) -> _Request: ...


class GmailService(Protocol):
    def users(self) -> _Users: ...


def build_mail_service(credentials: object, *, timeout_s: float = 20.0) -> GmailService:
    """Create the real Gmail API service (imported only now, to spare memory)."""
    discovery = importlib.import_module("googleapiclient.discovery")
    httplib2 = importlib.import_module("httplib2")
    google_auth_httplib2 = importlib.import_module("google_auth_httplib2")
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout_s))
    service: GmailService = discovery.build(
        "gmail", "v1", http=http, cache_discovery=False, static_discovery=True
    )
    return service


# --- reading a message -----------------------------------------------------------------------


def parse_sender(header: str) -> tuple[str, str]:
    """Return (address, display name); the address is empty unless there is exactly one."""
    found = getaddresses([header])
    if len(found) != 1:
        return "", ""
    name, address = parseaddr(header)
    address = address.strip().lower()
    return (address, name.strip()) if _ADDRESS.match(address) else ("", name.strip())


def authentication_result(headers: Sequence[tuple[str, str]]) -> bool | None:
    """SPF/DKIM verdict of Gmail's own (topmost) ``Authentication-Results`` header.

    ``None`` when there is no such header; ``False`` when the topmost one was not written by Gmail
    (a sender can add headers of their own, Gmail's is always first) or has no pass.
    """
    for name, value in headers:
        if name.lower() == "authentication-results":
            if not value.strip().lower().startswith("mx.google.com"):
                return False
            return any(result.lower() == "pass" for _, result in _AUTH_RESULT.findall(value))
    return None


def is_automatic(headers: Sequence[tuple[str, str]], sender: str) -> bool:
    """Bounces, bulk and auto-generated mail, which must not be answered or alerted about."""
    for name, value in headers:
        key, text = name.lower(), value.strip().lower()
        if key == "auto-submitted" and text != "no":
            return True
        if key == "precedence" and text in ("bulk", "junk", "list", "auto_reply"):
            return True
        if key in ("x-failed-recipients", "list-id", "x-autoreply", "x-autorespond"):
            return True
    return sender.startswith(_ROBOT_SENDERS)


def _decode(data: str, charset: str | None) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _charset(part: dict[str, Any]) -> str | None:
    for header in part.get("headers", []):
        if header.get("name", "").lower() == "content-type":
            found = re.search(r"charset=\"?([\w.:-]+)", header.get("value", ""), re.IGNORECASE)
            return found.group(1) if found else None
    return None


def _find_text(part: dict[str, Any], kind: str, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if part.get("mimeType", "").lower() == kind:
        data = part.get("body", {}).get("data")
        return _decode(data, _charset(part)) if isinstance(data, str) else None
    for child in part.get("parts", []) or []:
        found = _find_text(child, kind, depth + 1)
        if found is not None:
            return found
    return None


def plain_text_of(payload: dict[str, Any]) -> str:
    """The text of a message: the plain part, else the html part without its tags."""
    text = _find_text(payload, "text/plain")
    if text is None:
        html = _find_text(payload, "text/html")
        text = _TAGS.sub(" ", html) if html is not None else ""
    return text.strip()[:MAX_BODY_CHARS]


def parse_message(raw: dict[str, Any]) -> MailMessage:
    """Turn a Gmail ``messages.get(format="full")`` answer into a :class:`MailMessage`."""
    payload = raw.get("payload") or {}
    headers = [
        (str(h.get("name", "")), str(h.get("value", "")))
        for h in payload.get("headers", [])
        if isinstance(h, dict)
    ]

    def first(name: str) -> str:
        return next((v for n, v in headers if n.lower() == name), "")

    sender, display = parse_sender(first("from"))
    received: datetime | None
    try:
        received = parsedate_to_datetime(first("date")) if first("date") else None
    except (TypeError, ValueError):
        received = None
    return MailMessage(
        message_id=str(raw.get("id", "")),
        sender=sender,
        sender_name=display,
        to=first("delivered-to") or first("to"),
        subject=first("subject").strip(),
        body=plain_text_of(payload),
        received=received,
        automatic=is_automatic(headers, sender),
        authenticated=authentication_result(headers),
    )


# --- writing a message -----------------------------------------------------------------------


def build_raw_message(sender: str, mail: OutgoingMail) -> dict[str, str]:
    """The Gmail ``raw`` body for an outgoing plain-text message."""
    for label, address in (("sender", sender), ("recipient", mail.to)):
        if not _ADDRESS.match(address):
            raise MailError(f"the {label} address is not a single valid address")
    message = EmailMessage()
    message["To"] = mail.to
    message["From"] = sender
    message["Subject"] = re.sub(r"[\r\n]+", " ", mail.subject).strip()[:200]
    message["Auto-Submitted"] = "auto-generated"  # so that no other robot answers it
    message.set_content(mail.body[:MAX_SEND_CHARS])
    return {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}


# --- the client ------------------------------------------------------------------------------


def _translate(error: Exception) -> MailError | None:
    known = classify_error(error)
    if known is None:
        return None
    if isinstance(known, CalendarAuthError):
        return MailAuthError(str(known))
    if isinstance(known, CalendarUnavailable):
        return MailUnavailable(str(known))
    return MailError(str(known))


class GoogleMailClient:
    """Gmail over an injected API service."""

    def __init__(self, service: GmailService) -> None:
        self._service = service
        self._address: str | None = None

    def own_address(self) -> str:
        """The authorized account, which is also the sender of every mail we send (FR-GOOG-1a)."""
        if self._address is None:
            profile = self._call(lambda: self._service.users().getProfile(userId="me"))
            address = str(profile.get("emailAddress", "")).strip().lower()
            if not _ADDRESS.match(address):
                raise MailError("Gmail did not report the account address")
            self._address = address
        return self._address

    def list_inbox(self, limit: int = 20) -> InboxRead:
        messages = self._service.users().messages()
        listing = self._call(lambda: messages.list(userId="me", q=INBOX_QUERY, maxResults=limit))
        refs = [ref["id"] for ref in listing.get("messages", []) if isinstance(ref.get("id"), str)]
        found: list[MailMessage] = []
        for message_id in refs[:limit]:
            raw = self._fetch(messages, message_id)
            found.append(parse_message(raw))
        more = bool(listing.get("nextPageToken")) or len(refs) > limit
        return InboxRead(tuple(found), more)

    def _fetch(self, messages: _Messages, message_id: str) -> Any:
        return self._call(lambda: messages.get(userId="me", id=message_id, format="full"))

    def remove(self, message_id: str) -> None:
        messages = self._service.users().messages()
        self._call(lambda: messages.trash(userId="me", id=message_id))

    def send(self, mail: OutgoingMail) -> None:
        body = build_raw_message(self.own_address(), mail)
        messages = self._service.users().messages()
        self._call(lambda: messages.send(userId="me", body=body))
        logger.info(kv("mail_sent", subject_chars=len(mail.subject), body_chars=len(mail.body)))

    def _call(self, build: Callable[[], _Request]) -> Any:
        try:
            return build().execute(num_retries=RETRIES)
        except MailError:
            raise
        except Exception as error:
            translated = _translate(error)
            if translated is None:
                raise
            raise translated from None
