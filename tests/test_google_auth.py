"""Google authorization: old-token conversion, token file, status, consent flow (FR-GOOG-1..7)."""

import io
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from orchestrator.auth import main
from orchestrator.google_auth import (
    CALENDAR_SCOPE,
    GMAIL_SCOPE,
    SCOPES,
    AuthError,
    RevokedError,
    TokenState,
    authorize,
    convert_oauth2client,
    import_old_token,
    read_token_data,
    token_status,
    write_token,
)

SECRET = "refresh-SECRET-4711"

# the shape of a file written by oauth2client.file.Storage (the legacy tool)
OLD_TOKEN: dict[str, Any] = {
    "access_token": "ya29.old-access",
    "client_id": "123.apps.googleusercontent.com",
    "client_secret": "client-SECRET",
    "refresh_token": SECRET,
    "token_expiry": "2020-01-01T00:00:00Z",
    "token_uri": "https://oauth2.googleapis.com/token",
    "user_agent": "Google Calendar API .NET",
    "revoke_uri": "https://oauth2.googleapis.com/revoke",
    "id_token": None,
    "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
    "invalid": False,
    "_class": "OAuth2Credentials",
    "_module": "oauth2client.client",
}

NEW_TOKEN: dict[str, Any] = {
    "type": "authorized_user",
    "client_id": "1.apps.googleusercontent.com",
    "client_secret": "s",
    "refresh_token": SECRET,
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": list(SCOPES),
}


def test_the_scopes_are_calendar_read_write_and_full_gmail() -> None:
    assert SCOPES == (CALENDAR_SCOPE, GMAIL_SCOPE)
    assert CALENDAR_SCOPE.endswith("/auth/calendar")  # not .readonly (FR-GOOG-1)


def test_an_old_token_is_converted_without_the_obsolete_fields() -> None:
    converted = convert_oauth2client(OLD_TOKEN)
    assert converted["type"] == "authorized_user"
    assert converted["refresh_token"] == SECRET
    assert converted["client_id"] == OLD_TOKEN["client_id"]
    assert "access_token" not in converted
    assert "_class" not in converted


@pytest.mark.parametrize("key", ["refresh_token", "client_id", "client_secret"])
def test_an_old_token_without_a_required_value_is_refused(key: str) -> None:
    broken = {**OLD_TOKEN, key: None}
    with pytest.raises(AuthError) as caught:
        convert_oauth2client(broken)
    assert SECRET not in str(caught.value)


def test_an_old_read_only_token_is_reported_as_needing_more_permissions(tmp_path: Path) -> None:
    source, target = tmp_path / "old.json", tmp_path / "new" / "token.json"
    source.write_text(json.dumps(OLD_TOKEN), encoding="utf-8")
    status = import_old_token(source, target)
    assert status.state is TokenState.SCOPES_MISSING
    assert set(status.missing_scopes) == set(SCOPES)
    assert not status.usable
    assert read_token_data(target)["refresh_token"] == SECRET


def test_the_token_is_stored_with_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    write_token(path, NEW_TOKEN)
    write_token(path, {**NEW_TOKEN, "client_secret": "changed"})  # replaced, not appended
    assert json.loads(path.read_text(encoding="utf-8"))["client_secret"] == "changed"
    assert not list(tmp_path.glob("*.bak"))  # a backup would carry default permissions
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_status_of_missing_unreadable_and_incomplete_files(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    assert token_status(path).state is TokenState.MISSING
    path.write_text("not json " + SECRET, encoding="utf-8")
    bad = token_status(path)
    assert bad.state is TokenState.UNREADABLE
    assert SECRET not in bad.detail
    path.write_text(json.dumps({"client_id": "x"}), encoding="utf-8")
    assert token_status(path).state is TokenState.UNREADABLE
    path.write_text("[1, 2]", encoding="utf-8")
    assert token_status(path).state is TokenState.UNREADABLE


def test_a_complete_token_is_ok_and_google_is_asked_only_when_requested(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    write_token(path, NEW_TOKEN)
    asked: list[bool] = []
    assert token_status(path).state is TokenState.OK
    status = token_status(path, refresh=lambda data: asked.append(True))
    assert status.state is TokenState.OK
    assert asked == [True]


def test_revoked_and_unreachable_are_told_apart(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    write_token(path, NEW_TOKEN)

    def revoked(data: dict[str, Any]) -> None:
        raise RevokedError

    def offline(data: dict[str, Any]) -> None:
        raise OSError("no route")

    gone = token_status(path, refresh=revoked)
    assert gone.state is TokenState.REVOKED
    assert "In production" in gone.detail  # FR-GOOG-5
    assert not gone.usable
    away = token_status(path, refresh=offline)
    assert away.state is TokenState.OFFLINE
    assert away.usable  # keep running on the last valid files (FR-GOOG-4)


class FakeCredentials:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def to_json(self) -> str:
        return json.dumps(self._data)


class FakeFlow:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.kwargs: dict[str, Any] = {}

    def run_local_server(self, **kwargs: Any) -> FakeCredentials:
        self.kwargs = kwargs
        return FakeCredentials(self.data)


def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "client_secret.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_authorize_runs_the_flow_and_stores_the_token(tmp_path: Path) -> None:
    flow = FakeFlow({k: v for k, v in NEW_TOKEN.items() if k != "scopes"})
    seen: list[tuple[Path, tuple[str, ...]]] = []

    def factory(secret: Path, scopes: tuple[str, ...]) -> FakeFlow:
        seen.append((secret, scopes))
        return flow

    token = tmp_path / "token.json"
    status = authorize(
        secret_file(tmp_path), token, port=9999, open_browser=False, flow_factory=factory
    )
    assert status.state is TokenState.OK
    assert seen[0][1] == SCOPES
    assert flow.kwargs["port"] == 9999
    assert flow.kwargs["open_browser"] is False
    assert flow.kwargs["access_type"] == "offline"  # without it there is no refresh token
    assert read_token_data(token)["scopes"] == list(SCOPES)


def test_authorize_refuses_a_result_without_a_refresh_token(tmp_path: Path) -> None:
    flow = FakeFlow({"client_id": "x", "client_secret": "y"})
    with pytest.raises(AuthError, match="no refresh token"):
        authorize(secret_file(tmp_path), tmp_path / "t.json", flow_factory=lambda s, c: flow)
    assert not (tmp_path / "t.json").exists()


def test_authorize_needs_the_client_secret_file(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="client secret file not found"):
        authorize(tmp_path / "missing.json", tmp_path / "t.json")


# --- command line -----------------------------------------------------------------------------


def run(
    argv: list[str], env: dict[str, str] | None = None, flow: FakeFlow | None = None
) -> tuple[int, str]:
    out = io.StringIO()
    factory = None if flow is None else (lambda s, c: flow)
    code = main(argv, env=env or {}, out=out, flow_factory=factory)
    return code, out.getvalue()


def test_cli_requires_a_token_path() -> None:
    code, text = run([])
    assert code == 2
    assert "GOOGLE_TOKEN_FILE" in text


def test_cli_check_offline_reports_and_never_prints_secrets(tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    code, text = run(["--check", "--offline", "--token", str(token)])
    assert code == 1
    assert "missing" in text
    assert "authorize again" in text
    write_token(token, NEW_TOKEN)
    code, text = run(["--check", "--offline"], env={"GOOGLE_TOKEN_FILE": str(token)})
    assert code == 0
    assert SECRET not in text


def test_cli_imports_an_old_token_and_asks_for_new_permissions(tmp_path: Path) -> None:
    source = tmp_path / "old.json"
    source.write_text(json.dumps(OLD_TOKEN), encoding="utf-8")
    token = tmp_path / "token.json"
    code, text = run(["--import-oauth2client", str(source), "--token", str(token)])
    assert code == 1
    assert "scopes_missing" in text
    assert CALENDAR_SCOPE in text
    assert "authorize again" in text
    assert SECRET not in text


def test_cli_runs_the_flow_with_options_and_environment(tmp_path: Path) -> None:
    flow = FakeFlow({k: v for k, v in NEW_TOKEN.items() if k != "scopes"})
    token = tmp_path / "token.json"
    code, text = run(
        ["--no-browser", "--port", "8123"],
        env={
            "GOOGLE_TOKEN_FILE": str(token),
            "GOOGLE_CLIENT_SECRET_FILE": str(secret_file(tmp_path)),
        },
        flow=flow,
    )
    assert code == 0, text
    assert flow.kwargs["open_browser"] is False
    assert flow.kwargs["port"] == 8123
    assert token.exists()


def test_cli_reports_a_problem_without_a_traceback(tmp_path: Path) -> None:
    code, text = run(["--token", str(tmp_path / "t.json"), "--client-secret", str(tmp_path / "x")])
    assert code == 1
    assert text.startswith("error: client secret file not found")


def test_cli_needs_the_client_secret_to_authorize(tmp_path: Path) -> None:
    code, text = run(["--token", str(tmp_path / "t.json")])
    assert code == 2
    assert "GOOGLE_CLIENT_SECRET_FILE" in text


# --- one login for the mail, another for the calendar -------------------------------------------


def token_with(scope: str, refresh: str = SECRET) -> dict[str, Any]:
    return {**NEW_TOKEN, "refresh_token": refresh, "scopes": [scope]}


def test_each_purpose_asks_for_its_own_permission(tmp_path: Path) -> None:
    from orchestrator.google_auth import CALENDAR_READONLY_SCOPE, status_for

    mail, readonly, full = tmp_path / "m.json", tmp_path / "r.json", tmp_path / "f.json"
    write_token(mail, token_with(GMAIL_SCOPE))
    write_token(readonly, token_with(CALENDAR_READONLY_SCOPE))
    write_token(full, token_with(CALENDAR_SCOPE))
    expected = {
        (mail, "mail"): TokenState.OK,
        (mail, "calendar"): TokenState.SCOPES_MISSING,
        (mail, "both"): TokenState.SCOPES_MISSING,
        (readonly, "calendar"): TokenState.OK,
        (readonly, "mail"): TokenState.SCOPES_MISSING,
        (full, "calendar"): TokenState.OK,
        (full, "both"): TokenState.SCOPES_MISSING,
    }
    for (path, purpose), state in expected.items():
        assert status_for(path, purpose).state is state, (path.name, purpose)


def test_credentials_ask_only_for_what_their_token_was_granted(tmp_path: Path) -> None:
    from orchestrator.google_auth import CALENDAR_READONLY_SCOPE, granted_scopes, load_credentials

    path = tmp_path / "r.json"
    write_token(path, token_with(CALENDAR_READONLY_SCOPE))
    credentials = load_credentials(
        path, scopes=(), any_of=(CALENDAR_SCOPE, CALENDAR_READONLY_SCOPE)
    )
    assert list(credentials.scopes) == [CALENDAR_READONLY_SCOPE]
    assert granted_scopes(path) == {CALENDAR_READONLY_SCOPE}
    assert granted_scopes(tmp_path / "missing.json") == frozenset()
    with pytest.raises(AuthError, match="fewer permissions"):
        load_credentials(path, scopes=(GMAIL_SCOPE,))


def test_the_cli_checks_a_token_for_the_job_it_is_meant_for(tmp_path: Path) -> None:
    from orchestrator.google_auth import CALENDAR_READONLY_SCOPE

    path = tmp_path / "t.json"
    write_token(path, token_with(CALENDAR_READONLY_SCOPE))
    code, text = run(["--check", "--offline", "--token", str(path), "--for", "calendar"])
    assert code == 0 and "ok" in text
    code, text = run(["--check", "--offline", "--token", str(path)])  # default: both
    assert code == 1 and "scopes_missing" in text


def test_the_cli_imports_an_old_gmail_token_for_the_mail(tmp_path: Path) -> None:
    source = tmp_path / "old.json"
    source.write_text(json.dumps({**OLD_TOKEN, "scopes": [GMAIL_SCOPE]}), encoding="utf-8")
    token = tmp_path / "token.json"
    code, text = run(["--import-oauth2client", str(source), "--token", str(token), "--for", "mail"])
    assert code == 0 and "ok" in text and SECRET not in text
    assert read_token_data(token)["token_uri"] == "https://oauth2.googleapis.com/token"
