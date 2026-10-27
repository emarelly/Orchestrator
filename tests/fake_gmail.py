"""A fake Gmail API service for tests: no network, no credentials.

It has the shape that ``GoogleMailClient`` uses (``service.users().messages().list(...)`` and so
on) and keeps an inbox, a trash and a list of sent messages, so a test can read what was sent.
"""

import base64
import email
import email.policy
from email.message import EmailMessage
from typing import Any

from tests.fake_google import FakeHttpError


def b64(text: str, charset: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(charset)).decode("ascii").rstrip("=")


def gmail_message(
    message_id: str,
    sender: str,
    subject: str,
    body: str = "",
    *,
    extra_headers: list[tuple[str, str]] | None = None,
    authenticated: str | None = "mx.google.com; dkim=pass header.i=@example.com; spf=pass",
    html: str | None = None,
    multipart: bool = True,
) -> dict[str, Any]:
    """A message as ``messages.get(format="full")`` returns it."""
    headers: list[dict[str, str]] = []
    if authenticated is not None:
        headers.append({"name": "Authentication-Results", "value": authenticated})
    headers += [
        {"name": "From", "value": sender},
        {"name": "Delivered-To", "value": "orchestrator@example.com"},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Sat, 24 Oct 2026 16:41:00 +0300"},
    ]
    headers += [{"name": n, "value": v} for n, v in extra_headers or []]
    text_part: dict[str, Any] = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="UTF-8"'}],
        "body": {"data": b64(body)},
    }
    parts = [text_part]
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    if multipart:
        payload: dict[str, Any] = {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": parts,
        }
    else:
        payload = {**text_part, "headers": headers + text_part["headers"]}
    return {"id": message_id, "payload": payload}


class _Request:
    def __init__(self, service: "FakeGmailService", verb: str, args: dict[str, Any]) -> None:
        self._service, self._verb, self._args = service, verb, args

    def execute(self, num_retries: int = 0) -> Any:
        return self._service.run(self._verb, self._args, num_retries)


class _Messages:
    def __init__(self, service: "FakeGmailService") -> None:
        self._service = service

    def list(self, **kwargs: Any) -> _Request:
        return _Request(self._service, "list", kwargs)

    def get(self, **kwargs: Any) -> _Request:
        return _Request(self._service, "get", kwargs)

    def trash(self, **kwargs: Any) -> _Request:
        return _Request(self._service, "trash", kwargs)

    def send(self, **kwargs: Any) -> _Request:
        return _Request(self._service, "send", kwargs)


class _Users:
    def __init__(self, service: "FakeGmailService") -> None:
        self._service = service

    def messages(self) -> _Messages:
        return _Messages(self._service)

    def getProfile(self, **kwargs: Any) -> _Request:
        return _Request(self._service, "profile", kwargs)


class FakeGmailService:
    def __init__(self, address: str = "orchestrator@example.com") -> None:
        self.address = address
        self.inbox: dict[str, dict[str, Any]] = {}
        self.trash: list[str] = []
        self.sent: list[EmailMessage] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None
        self.retries: list[int] = []

    def users(self) -> _Users:
        return _Users(self)

    def add(self, message: dict[str, Any]) -> None:
        self.inbox[message["id"]] = message

    def run(self, verb: str, args: dict[str, Any], retries: int) -> Any:
        self.calls.append((verb, args))
        self.retries.append(retries)
        if self.error is not None:
            raise self.error
        if verb == "profile":
            return {"emailAddress": self.address}
        if verb == "list":
            ids = list(self.inbox)
            page = ids[: args["maxResults"]]
            answer: dict[str, Any] = {"messages": [{"id": i} for i in page]}
            if len(ids) > len(page):
                answer["nextPageToken"] = "more"
            return answer if page else {}
        if verb == "get":
            if args["id"] not in self.inbox:
                raise FakeHttpError(404)
            return self.inbox[args["id"]]
        if verb == "trash":
            if args["id"] not in self.inbox:
                raise FakeHttpError(404)
            self.trash.append(args["id"])
            del self.inbox[args["id"]]
            return {}
        raw = base64.urlsafe_b64decode(args["body"]["raw"] + "==")
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        assert isinstance(parsed, EmailMessage)
        self.sent.append(parsed)
        return {"id": f"sent-{len(self.sent)}"}
