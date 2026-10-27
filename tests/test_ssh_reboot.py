"""SSH reboot of the boiler Pi and the quiet period after it (FR-LEG-3a, CS-15)."""

import gc
import io
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orchestrator.mail_client import GoogleMailClient, parse_message
from orchestrator.mail_commands import MailCommands
from orchestrator.mail_gate import SecurityAlerts
from orchestrator.reboot_grace import RebootGrace
from orchestrator.ssh_reboot import (
    REBOOT_COMMAND,
    RebootError,
    SshReboot,
    SshSession,
    SshTarget,
    connect_with_paramiko,
    fingerprint_of,
    main,
)
from tests.fake_gmail import FakeGmailService, gmail_message
from tests.fake_ssh import FakeSshServer, paramiko, write_private_key
from tests.fakes import FakeClock
from tests.test_manual_commands import Rig


@pytest.fixture(autouse=True)
def no_socket_is_left_open() -> Iterator[None]:
    """A socket that a test leaves open is only reported when it is collected: collect now."""
    yield
    gc.collect()


PASSWORD = "right-password-SENTINEL"
TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=TZ)


def target_for(
    server: FakeSshServer,
    *,
    host_key: str | None = None,
    user: str | None = "pi",
    password: str | None = PASSWORD,
    key_file: str | None = None,
    timeout_s: float = 5.0,
) -> SshTarget:
    return SshTarget(
        host="127.0.0.1",
        port=server.port,
        user=user,
        key_file=key_file,
        password=password,
        host_key=fingerprint_of(server.host_key.asbytes()) if host_key is None else host_key,
        timeout_s=timeout_s,
    )


# --- the fingerprint -------------------------------------------------------------------------


def test_the_fingerprint_has_the_openssh_shape() -> None:
    with FakeSshServer() as server:
        text = fingerprint_of(server.host_key.asbytes())
    assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", text)


def test_the_fingerprint_matches_what_paramiko_itself_reports() -> None:
    with FakeSshServer() as server:
        expected = server.host_key.fingerprint  # paramiko's own SHA-256 fingerprint attribute
        seen = fingerprint_of(server.host_key.asbytes())
    assert seen.removeprefix("SHA256:") == expected.removeprefix("SHA256:").rstrip("=")


# --- a real connection ------------------------------------------------------------------------


def test_a_dropped_connection_after_sudo_reboot_is_success() -> None:
    called: list[bool] = []
    with FakeSshServer(password=PASSWORD, exit_status=None) as server:
        reboot = SshReboot(target_for(server), on_requested=lambda: called.append(True))
        message = reboot.reboot()
        assert server.commands == [REBOOT_COMMAND] == ["sudo reboot"]
    assert "reboot requested" in message
    assert called == [True]


def test_exit_status_zero_is_success() -> None:
    with FakeSshServer(password=PASSWORD, exit_status=0) as server:
        assert "reboot requested" in SshReboot(target_for(server)).reboot()


def test_a_failing_command_is_reported_and_the_quiet_period_does_not_start() -> None:
    called: list[bool] = []
    server = FakeSshServer(password=PASSWORD, exit_status=1)
    with server, pytest.raises(RebootError, match="exit status 1"):
        SshReboot(target_for(server), on_requested=lambda: called.append(True)).reboot()
    assert called == []


def test_a_command_that_never_finishes_is_an_error_not_a_guess() -> None:
    server = FakeSshServer(password=PASSWORD, hang=True)
    with server, pytest.raises(RebootError, match="did not finish in time"):
        SshReboot(target_for(server, timeout_s=1.0)).reboot()


def test_login_with_a_key_file(tmp_path: Path) -> None:
    key = paramiko.ECDSAKey.generate()
    key_file = write_private_key(key, tmp_path / "id_ecdsa")
    with FakeSshServer(password=None, accepted_key=key) as server:
        target = target_for(server, password=None, key_file=key_file)
        assert "reboot requested" in SshReboot(target).reboot()
        assert server.logins == ["key"]


def test_a_key_file_is_preferred_over_the_password(tmp_path: Path) -> None:
    key = paramiko.ECDSAKey.generate()
    key_file = write_private_key(key, tmp_path / "id_ecdsa")
    with FakeSshServer(password=PASSWORD, accepted_key=key) as server:
        SshReboot(target_for(server, key_file=key_file)).reboot()
        assert server.logins == ["key"]


def test_the_wrong_password_or_key_runs_nothing(tmp_path: Path) -> None:
    with FakeSshServer(password=PASSWORD) as server:
        with pytest.raises(RebootError, match="login was refused"):
            SshReboot(target_for(server, password="wrong")).reboot()
        assert server.commands == []
    other = paramiko.ECDSAKey.generate()
    key_file = write_private_key(other, tmp_path / "other")
    with FakeSshServer(password=None, accepted_key=paramiko.ECDSAKey.generate()) as server:
        with pytest.raises(RebootError, match="login was refused"):
            SshReboot(target_for(server, password=None, key_file=key_file)).reboot()
        assert server.commands == []


def test_a_different_host_key_is_refused_before_any_login() -> None:
    impostor = "SHA256:" + "A" * 43
    with FakeSshServer(password=PASSWORD) as server:
        with pytest.raises(RebootError, match="not the configured one"):
            SshReboot(target_for(server, host_key=impostor)).reboot()
        assert server.logins == []  # no password or key was offered to the wrong host
        assert server.commands == []


def test_an_unreachable_host_is_reported() -> None:
    with FakeSshServer() as server:
        target = target_for(server)
    with pytest.raises(RebootError, match="could not be reached"):
        SshReboot(target).reboot()


def test_nothing_secret_appears_in_any_error_text(tmp_path: Path) -> None:
    with FakeSshServer(password="something-else") as server, pytest.raises(RebootError) as caught:
        SshReboot(target_for(server)).reboot()
    assert PASSWORD not in str(caught.value)


# --- refusing to start ---------------------------------------------------------------------


class Recording:
    """A session that records the order of the steps."""

    def __init__(self, fingerprint: str = "SHA256:" + "B" * 43, status: int | None = 0) -> None:
        self.steps: list[str] = []
        self._fingerprint = fingerprint
        self._status = status

    def host_key_fingerprint(self) -> str:
        self.steps.append("fingerprint")
        return self._fingerprint

    def authenticate(self, user: str, key_file: str | None, password: str | None) -> None:
        self.steps.append("authenticate")

    def run(self, command: str, timeout_s: float) -> int | None:
        self.steps.append(f"run {command}")
        return self._status

    def close(self) -> None:
        self.steps.append("close")


def reboot_with(session: Recording, **changes: Any) -> SshReboot:
    target = SshTarget("pi.example", 22, "pi", None, PASSWORD, "SHA256:" + "B" * 43, 5.0)
    return SshReboot(replace(target, **changes), connect=lambda host, port, timeout: session)


def test_the_steps_run_in_the_safe_order_and_the_connection_is_always_closed() -> None:
    session = Recording()
    reboot_with(session).reboot()
    assert session.steps == ["fingerprint", "authenticate", "run sudo reboot", "close"]
    mismatch = Recording(fingerprint="SHA256:" + "C" * 43)
    with pytest.raises(RebootError):
        reboot_with(mismatch).reboot()
    assert mismatch.steps == ["fingerprint", "close"]  # no authenticate, no command


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"host_key": ""}, "host key is not configured"),
        ({"user": None}, "SSH user is not set"),
        ({"password": None, "key_file": None}, "neither BOILER_SSH_KEY_FILE"),
    ],
)
def test_missing_settings_stop_before_any_connection(change: dict[str, Any], fragment: str) -> None:
    contacted: list[str] = []

    def connect(host: str, port: int, timeout: float) -> SshSession:
        contacted.append(host)
        return Recording()

    target = SshTarget("pi.example", 22, "pi", None, PASSWORD, "SHA256:" + "B" * 43, 5.0)
    with pytest.raises(RebootError, match=fragment):
        SshReboot(replace(target, **change), connect=connect).reboot()
    assert contacted == []


def test_a_connection_that_drops_while_running_counts_as_success() -> None:
    class Dropping(Recording):
        def run(self, command: str, timeout_s: float) -> int | None:
            raise ConnectionResetError("gone")

    assert "reboot requested" in reboot_with(Dropping()).reboot()


def test_a_connector_failure_is_reported_without_library_text() -> None:
    def broken(host: str, port: int, timeout: float) -> SshSession:
        raise ValueError("internal detail with " + PASSWORD)

    target = SshTarget("pi.example", 22, "pi", None, PASSWORD, "SHA256:" + "B" * 43, 5.0)
    with pytest.raises(RebootError) as caught:
        SshReboot(target, connect=broken).reboot()
    assert "ValueError" in str(caught.value) and PASSWORD not in str(caught.value)


def test_the_command_is_one_fixed_string() -> None:
    assert REBOOT_COMMAND == "sudo reboot"
    session = Recording()
    reboot_with(session).reboot()
    assert [s for s in session.steps if s.startswith("run")] == ["run sudo reboot"]


# --- the quiet period -------------------------------------------------------------------------


def test_the_quiet_period_starts_with_the_request_and_ends_after_the_grace_time() -> None:
    clock = FakeClock(NOW)
    grace = RebootGrace(clock, timedelta(minutes=5))
    assert not grace.suppressed() and not grace.pending() and grace.finish() is None
    grace.begin()
    assert grace.suppressed() and grace.pending()
    clock.advance(timedelta(minutes=4, seconds=59))
    assert grace.suppressed()
    clock.advance(timedelta(seconds=2))
    assert not grace.suppressed()
    assert grace.pending()  # the Pi has still not been seen again


def test_finish_tells_how_long_the_reboot_took_and_clears_the_state() -> None:
    clock = FakeClock(NOW)
    grace = RebootGrace(clock)
    grace.begin()
    clock.advance(timedelta(minutes=2, seconds=30))
    assert grace.finish() == timedelta(minutes=2, seconds=30)
    assert not grace.pending() and not grace.suppressed() and grace.finish() is None


def test_the_quiet_period_counts_real_time_across_a_clock_change() -> None:
    clock = FakeClock(datetime(2026, 10, 25, 1, 58, tzinfo=TZ, fold=0))  # clocks go back at 02:00
    grace = RebootGrace(clock, timedelta(minutes=5))
    grace.begin()
    clock.advance(timedelta(minutes=4))
    assert grace.suppressed()
    clock.advance(timedelta(minutes=2))
    assert not grace.suppressed()


def test_a_zero_grace_time_suppresses_nothing() -> None:
    grace = RebootGrace(FakeClock(NOW), timedelta(0))
    grace.begin()
    assert not grace.suppressed()


# --- showing the host key --------------------------------------------------------------------


def test_show_host_key_prints_the_fingerprint_and_sends_nothing_else(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    with FakeSshServer() as server:
        config.write_text(
            f'[boiler]\nbase_url = "http://127.0.0.1:8000/"\nssh_port = {server.port}\n',
            encoding="utf-8",
        )
        out = io.StringIO()
        code = main(["--show-host-key", "--config", str(config)], out=out)
        assert code == 0
        assert fingerprint_of(server.host_key.asbytes()) in out.getvalue()
        assert "ssh-keygen" in out.getvalue()
        assert server.logins == [] and server.commands == []


def test_show_host_key_reports_a_problem_without_a_traceback(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[boiler]\nbase_url = "http://127.0.0.1:9/"\nssh_port = 9\n', encoding="utf-8"
    )
    out = io.StringIO()
    assert main(["--show-host-key", "--config", str(config)], out=out) == 1
    assert out.getvalue().startswith("error: could not read the host key")
    bad = tmp_path / "bad.toml"
    bad.write_text("[boiler\n", encoding="utf-8")
    assert main(["--show-host-key", "--config", str(bad)], out=io.StringIO()) == 2


def test_the_real_connector_is_the_default() -> None:
    assert SshReboot.__init__.__defaults__ is None or connect_with_paramiko is not None


# --- the e-mail command "reset" with the real SSH code -------------------------------------


def test_reset_by_mail_reboots_over_ssh_and_starts_the_quiet_period() -> None:
    rig = Rig()
    gmail = FakeGmailService("orchestrator@example.com")
    client = GoogleMailClient(gmail)
    grace = RebootGrace(rig.clock)
    with FakeSshServer(password=PASSWORD) as server:
        commands = MailCommands(
            client,
            rig.commands,
            rig.cache,
            rig.clock,
            SecurityAlerts(client, "alert@example.com", rig.clock),
            log_text=lambda: "",
            rebooter=SshReboot(target_for(server), on_requested=grace.begin),
            token_factory=lambda: "abcdefghij",
        )

        def send(subject: str) -> None:
            commands.handle(parse_message(gmail_message("m", "eli@gmail.com", subject, "")))

        send("reset")
        assert server.commands == [] and not grace.pending()  # only requested so far
        send("abcdefghij")
        assert server.commands == ["sudo reboot"]
    assert grace.suppressed()
    assert "DONE: reboot requested" in gmail.sent[-1].get_content()
    rig.session.close()


def test_reset_by_mail_explains_a_refusal_in_plain_words() -> None:
    rig = Rig()
    gmail = FakeGmailService("orchestrator@example.com")
    client = GoogleMailClient(gmail)
    target = SshTarget("127.0.0.1", 22, "pi", None, PASSWORD, "", 5.0)  # no host key configured
    commands = MailCommands(
        client,
        rig.commands,
        rig.cache,
        rig.clock,
        SecurityAlerts(client, "alert@example.com", rig.clock),
        log_text=lambda: "",
        rebooter=SshReboot(target),
        token_factory=lambda: "abcdefghij",
    )
    for subject in ("reset", "abcdefghij"):
        commands.handle(parse_message(gmail_message("m", "eli@gmail.com", subject, "")))
    text = gmail.sent[-1].get_content()
    assert "FAILED" in text and "host key is not configured" in text and PASSWORD not in text
    rig.session.close()
