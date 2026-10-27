"""Google authorization: token file, conversion of the old format, expiry checks (FR-GOOG-1..7).

The token file is the ``authorized_user`` JSON that ``google-auth`` reads. It holds a refresh token,
so it is written with mode 600 and its content is never logged or put in an error message. The
Google libraries are imported only when they are needed, to spare memory on the Pi 3.
"""

import importlib
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from orchestrator.files import write_atomic

log = logging.getLogger(__name__)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GMAIL_SCOPE = "https://mail.google.com/"
SCOPES = (CALENDAR_SCOPE, GMAIL_SCOPE)
TOKEN_URI = "https://oauth2.googleapis.com/token"

_REQUIRED = ("client_id", "client_secret", "refresh_token")


class AuthError(Exception):
    """A problem with the authorization files; the text never contains a secret."""


class TokenState(StrEnum):
    OK = "ok"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    SCOPES_MISSING = "scopes_missing"
    REVOKED = "revoked"  # Google no longer accepts the refresh token
    OFFLINE = "offline"  # Google could not be reached, so the token could not be checked


@dataclass(frozen=True, slots=True)
class TokenStatus:
    state: TokenState
    detail: str
    missing_scopes: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.state in (TokenState.OK, TokenState.OFFLINE)


def convert_oauth2client(data: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a token file written by the old ``oauth2client`` library into the new format.

    The old scopes are kept as they were. A Gmail token and a read-only Calendar token stay two
    separate files (a refresh token belongs to its scopes); the program uses each for what it can do
    (FR-GOOG-1b, FR-GOOG-4a). The current token endpoint replaces the old one in the old file.
    """
    if "refresh_token" not in data or not data.get("refresh_token"):
        raise AuthError("the old token file has no refresh token; authorize again")
    converted: dict[str, Any] = {
        "type": "authorized_user",
        "client_id": data.get("client_id"),
        "client_secret": data.get("client_secret"),
        "refresh_token": data["refresh_token"],
        "token_uri": TOKEN_URI,
    }
    scopes = data.get("scopes")
    if isinstance(scopes, str):
        scopes = scopes.split()
    if scopes:
        converted["scopes"] = sorted(str(scope) for scope in scopes)
    for key in _REQUIRED:
        if not isinstance(converted.get(key), str) or not converted[key]:
            raise AuthError(f"the old token file has no usable {key}; authorize again")
    return converted


def read_token_data(path: Path) -> dict[str, Any]:
    """Read the token JSON; raises ``AuthError`` (without the content) when it is not usable."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AuthError("token file not found") from None
    except (OSError, UnicodeDecodeError):
        raise AuthError("token file cannot be read") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise AuthError("token file is not valid JSON") from None
    if not isinstance(data, dict):
        raise AuthError("token file has an unexpected shape")
    return data


def write_token(path: Path, data: Mapping[str, Any]) -> None:
    """Store the token with mode 600 (FR-GOOG-2); the file is replaced atomically, no backup."""
    payload = json.dumps(dict(data), indent=2, sort_keys=True).encode("utf-8")
    write_atomic(path, payload, keep_backup=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        log.warning("event=token_chmod_failed")


PURPOSES = ("both", "mail", "calendar")


def status_for(
    path: Path, purpose: str = "both", *, refresh: Callable[[dict[str, Any]], None] | None = None
) -> TokenStatus:
    """Check a token for what it is to be used for: ``mail``, ``calendar`` or ``both``.

    A calendar token may carry the read-only or the full permission; ``both`` needs both services.
    """
    if purpose == "mail":
        return token_status(path, scopes=(GMAIL_SCOPE,), refresh=refresh)
    if purpose == "calendar":
        return token_status(
            path, scopes=(), any_of=(CALENDAR_SCOPE, CALENDAR_READONLY_SCOPE), refresh=refresh
        )
    return token_status(path, refresh=refresh)


def import_old_token(source: Path, target: Path, purpose: str = "both") -> TokenStatus:
    """Convert an ``oauth2client`` token file and store it as the new token."""
    write_token(target, convert_oauth2client(read_token_data(source)))
    return status_for(target, purpose)


def token_status(
    path: Path,
    *,
    scopes: tuple[str, ...] = SCOPES,
    any_of: tuple[str, ...] = (),
    refresh: Callable[[dict[str, Any]], None] | None = None,
) -> TokenStatus:
    """Check the token file; with ``refresh`` also ask Google whether the token still works.

    ``scopes`` must all be granted; with ``any_of`` instead at least one of those must be (a
    calendar may be read with the read-only or the full permission).

    ``refresh`` receives the token data and raises ``RevokedError`` or ``OSError`` (FR-GOOG-5).
    """
    try:
        data = read_token_data(path)
    except AuthError as error:
        state = TokenState.MISSING if "not found" in str(error) else TokenState.UNREADABLE
        return TokenStatus(state, str(error))
    missing_fields = [key for key in _REQUIRED if not data.get(key)]
    if missing_fields:
        return TokenStatus(TokenState.UNREADABLE, "token file lacks " + ", ".join(missing_fields))
    granted = data.get("scopes")
    if granted:
        absent = tuple(scope for scope in scopes if scope not in granted)
        if any_of:
            absent = () if any(scope in granted for scope in any_of) else any_of
        if absent:
            return TokenStatus(
                TokenState.SCOPES_MISSING, "token was granted fewer permissions", absent
            )
    if refresh is None:
        return TokenStatus(TokenState.OK, "token file looks complete (not checked with Google)")
    try:
        refresh(data)
    except RevokedError:
        return TokenStatus(
            TokenState.REVOKED,
            "Google rejected the token (revoked, expired, or the consent screen is in "
            "'Testing' mode: set it to 'In production'); authorize again",
        )
    except OSError:
        return TokenStatus(TokenState.OFFLINE, "Google could not be reached; try again later")
    return TokenStatus(TokenState.OK, "Google accepted the token")


class RevokedError(Exception):
    """Google answered ``invalid_grant`` for the refresh token."""


def refresh_with_google(data: dict[str, Any]) -> None:
    """Ask Google for a new access token with the real libraries (network)."""
    credentials_module = importlib.import_module("google.oauth2.credentials")
    requests_module = importlib.import_module("google.auth.transport.requests")
    exceptions = importlib.import_module("google.auth.exceptions")
    credentials = credentials_module.Credentials.from_authorized_user_info(data)
    try:
        credentials.refresh(requests_module.Request())
    except exceptions.RefreshError as error:
        # google-auth reports the OAuth error name in the exception text, never a secret
        if "invalid_grant" in str(error):
            raise RevokedError from None
        raise AuthError("Google refused the token refresh") from None
    except exceptions.TransportError:
        raise OSError("transport error") from None


def granted_scopes(path: Path) -> frozenset[str]:
    """The permissions written in a token file (empty if unknown or unreadable)."""
    try:
        granted = read_token_data(path).get("scopes") or []
    except AuthError:
        return frozenset()
    return frozenset(str(scope) for scope in granted)


def load_credentials(
    path: Path, *, scopes: tuple[str, ...] = SCOPES, any_of: tuple[str, ...] = ()
) -> Any:
    """Return ``google.oauth2`` credentials that refresh themselves (FR-GOOG-2).

    The credentials ask for the permissions that the token itself was granted, never for more.
    """
    status = token_status(path, scopes=scopes, any_of=any_of)
    if not status.usable:
        raise AuthError(status.detail)
    data = read_token_data(path)
    own = [str(scope) for scope in data.get("scopes") or []]
    credentials_module = importlib.import_module("google.oauth2.credentials")
    return credentials_module.Credentials.from_authorized_user_info(
        data, scopes=own or list(scopes or any_of)
    )


class _Flow(Protocol):
    def run_local_server(self, **kwargs: Any) -> Any: ...


FlowFactory = Callable[[Path, tuple[str, ...]], _Flow]


def _installed_app_flow(client_secret: Path, scopes: tuple[str, ...]) -> _Flow:
    module = importlib.import_module("google_auth_oauthlib.flow")
    flow: _Flow = module.InstalledAppFlow.from_client_secrets_file(
        str(client_secret), scopes=list(scopes)
    )
    return flow


def authorize(
    client_secret: Path,
    token: Path,
    *,
    port: int = 8080,
    open_browser: bool = True,
    flow_factory: FlowFactory = _installed_app_flow,
) -> TokenStatus:
    """Run the installed-application consent flow and store the token (FR-GOOG-3)."""
    if not client_secret.is_file():
        raise AuthError("client secret file not found")
    flow = flow_factory(client_secret, SCOPES)
    credentials = flow.run_local_server(
        host="localhost",
        port=port,
        open_browser=open_browser,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Open this address in a browser to authorize: {url}",
        success_message="Authorization done. You can close this window.",
    )
    data = json.loads(credentials.to_json())
    if not data.get("refresh_token"):
        raise AuthError("Google returned no refresh token; run the tool again")
    data.setdefault("scopes", list(SCOPES))
    write_token(token, data)
    return token_status(token)
