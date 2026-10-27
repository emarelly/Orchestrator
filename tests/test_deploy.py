"""The systemd unit, the install and rollback scripts and their files (OPS-1..5, CS-16)."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import config as config_module
from orchestrator.config import load_config
from orchestrator.google_auth import (
    CALENDAR_READONLY_SCOPE,
    GMAIL_SCOPE,
    TokenState,
    read_token_data,
    status_for,
)
from orchestrator.supervisor import CHECK_INTERVAL_S

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
GIT_BASH = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)


def find_bash() -> str:
    """bash for running the scripts: Git's on Windows (System32's is only the WSL launcher)."""
    for candidate in GIT_BASH:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("bash")
    assert found, "bash is needed to test the install scripts (on Windows: Git for Windows)"
    return found


def run_script(
    name: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    script = (DEPLOY / name).as_posix()
    return subprocess.run(
        [find_bash(), script, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **env} if env else None,
    )


def unit() -> dict[str, list[str]]:
    """The unit file as {key: [values...]} (a key may appear more than once)."""
    values: dict[str, list[str]] = {}
    for line in (DEPLOY / "orchestrator.service").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), []).append(value.strip())
    return values


# --- the unit ---------------------------------------------------------------------------------


def test_the_unit_follows_the_service_requirements() -> None:
    u = unit()
    expected = {
        "Type": "notify",
        "User": "orchestrator",
        "Restart": "on-failure",
        "RestartSec": "10",
        "TimeoutStopSec": "15",
        "WatchdogSec": "60",
        "StartLimitIntervalSec": "600",
        "StartLimitBurst": "5",
        "MemoryMax": "300M",
        "EnvironmentFile": "/etc/orchestrator/orchestrator.env",
    }
    for key, value in expected.items():
        assert u[key] == [value], key


def test_the_unit_waits_for_the_network_and_for_the_clock() -> None:
    u = unit()
    for key in ("After", "Wants"):
        assert "network-online.target" in u[key][0] and "time-sync.target" in u[key][0]


def test_the_unit_is_hardened_and_can_write_only_its_own_folders() -> None:
    u = unit()
    assert u["NoNewPrivileges"] == ["yes"]
    assert u["ProtectSystem"] == ["strict"]
    assert u["ProtectHome"] == ["read-only"]
    assert u["PrivateTmp"] == ["yes"]
    assert u["ReadWritePaths"] == ["/var/lib/orchestrator /var/log/orchestrator"]
    assert u["User"] != ["root"] and "User" in u


def test_a_leak_is_slowed_down_before_it_is_killed() -> None:
    u = unit()
    high, maximum = int(u["MemoryHigh"][0][:-1]), int(u["MemoryMax"][0][:-1])
    assert high < maximum


def test_the_program_runs_from_the_current_folder_with_its_own_python() -> None:
    u = unit()
    assert u["WorkingDirectory"] == ["/opt/orchestrator/current"]
    assert u["ExecStart"] == ["/opt/orchestrator/current/venv/bin/python -m orchestrator"]
    assert "ORCHESTRATOR_CONFIG=/etc/orchestrator/config.toml" in u["Environment"]


def test_the_watchdog_time_is_safe_for_the_supervisor_checks() -> None:
    watchdog = int(unit()["WatchdogSec"][0])
    assert watchdog / 2 >= CHECK_INTERVAL_S * 3  # pinged well within half the watchdog time


def test_the_unit_has_unix_line_endings() -> None:
    assert b"\r" not in (DEPLOY / "orchestrator.service").read_bytes()
    for script in ("install.sh", "rollback.sh"):
        assert b"\r" not in (DEPLOY / script).read_bytes(), script


# --- the files that are installed ---------------------------------------------------------------


def test_the_pi_configuration_loads_and_matches_the_unit() -> None:
    config = load_config(DEPLOY / "config.pi.toml", env={})
    writable = unit()["ReadWritePaths"][0].split()
    assert config.general.data_dir in writable and config.general.log_dir in writable
    assert config.calendar.write_back is False and config.mqtt.enabled is False
    assert config.general.dry_run is False


def test_the_environment_template_holds_no_secret_values() -> None:
    known = {*config_module.SECRET_VARIABLES, config_module.ENV_DRY_RUN}
    lines = (DEPLOY / "orchestrator.env.example").read_text(encoding="utf-8").splitlines()
    active = {}
    for line in lines:
        text = line.strip().lstrip("#").strip() if line.startswith("#") else line.strip()
        if re.fullmatch(r"[A-Z_]+=.*", text):
            key, _, value = text.partition("=")
            assert key in known, key
            if not line.startswith("#"):
                active[key] = value
    for key, value in active.items():
        assert "PASSWORD" not in key or value == "", key
        if key.endswith("_FILE"):
            assert value.startswith("/var/lib/orchestrator/"), key
    assert "DRY_RUN" not in active  # a real install is not a dry run


def test_every_variable_the_program_reads_is_in_the_template() -> None:
    text = (DEPLOY / "orchestrator.env.example").read_text(encoding="utf-8")
    for name in config_module.SECRET_VARIABLES:
        assert name in text, name


# --- the scripts ----------------------------------------------------------------------------------


@pytest.mark.parametrize("script", ["install.sh", "rollback.sh"])
def test_the_scripts_have_no_syntax_errors(script: str) -> None:
    done = subprocess.run(
        [find_bash(), "-n", (DEPLOY / script).as_posix()],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


def test_the_pi_install_check_script_has_no_syntax_errors() -> None:
    done = subprocess.run(
        [find_bash(), "-n", (ROOT / "tools" / "check_pi_install.sh").as_posix()],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("script", ["install.sh", "rollback.sh"])
def test_help_and_unknown_options(script: str) -> None:
    assert run_script(script, "--help").returncode == 0
    bad = run_script(script, "--frobnicate")
    assert bad.returncode != 0 and "unknown option" in bad.stderr


def staged(tmp_path: Path) -> Path:
    return tmp_path / "root"


def install(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    # the real logins in the source folder must never be copied by a test: no import unless asked
    options = extra if "--google-from" in extra else (*extra, "--no-google-import")
    return run_script(
        "install.sh", "--destdir", staged(tmp_path).as_posix(), "--no-venv", "--no-start", *options
    )


def test_a_first_install_lays_out_everything(tmp_path: Path) -> None:
    done = install(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    root = staged(tmp_path)
    assert (root / "opt/orchestrator/current/orchestrator/__init__.py").exists()
    assert (root / "opt/orchestrator/current/RELEASE").exists()
    assert not list((root / "opt/orchestrator/current").rglob("__pycache__"))
    assert (
        (root / "etc/orchestrator/config.toml")
        .read_text(encoding="utf-8")
        .startswith("# Configuration")
    )
    assert "BOILER_SSH_USER" in (root / "etc/orchestrator/orchestrator.env").read_text(
        encoding="utf-8"
    )
    assert (root / "etc/systemd/system/orchestrator.service").read_bytes() == (
        DEPLOY / "orchestrator.service"
    ).read_bytes()
    for folder in (
        "var/lib/orchestrator/google",
        "var/lib/orchestrator/ssh",
        "var/log/orchestrator",
    ):
        assert (root / folder).is_dir()
    assert not (root / "opt/orchestrator/previous").exists()
    assert "(staging) skipped: systemctl enable" in done.stdout  # no real system was touched


def test_an_update_keeps_the_configuration_the_secrets_and_the_data(tmp_path: Path) -> None:
    assert install(tmp_path).returncode == 0
    root = staged(tmp_path)
    (root / "etc/orchestrator/config.toml").write_text("# mine\n", encoding="utf-8")
    (root / "etc/orchestrator/orchestrator.env").write_text("UI_PASSWORD=x\n", encoding="utf-8")
    (root / "var/lib/orchestrator/Cal.json").write_text("[]", encoding="utf-8")
    (root / "var/lib/orchestrator/google/token.json").write_text("{}", encoding="utf-8")
    assert install(tmp_path).returncode == 0
    assert (root / "etc/orchestrator/config.toml").read_text(encoding="utf-8") == "# mine\n"
    assert (root / "etc/orchestrator/orchestrator.env").read_text(
        encoding="utf-8"
    ) == "UI_PASSWORD=x\n"
    assert (root / "var/lib/orchestrator/Cal.json").exists()
    assert (root / "var/lib/orchestrator/google/token.json").exists()


def test_an_update_keeps_the_old_program_as_previous_and_rollback_swaps_them(
    tmp_path: Path,
) -> None:
    assert install(tmp_path).returncode == 0
    root = staged(tmp_path)
    (root / "opt/orchestrator/current/MARK").write_text("first", encoding="utf-8")
    assert install(tmp_path).returncode == 0
    assert (root / "opt/orchestrator/previous/MARK").read_text(encoding="utf-8") == "first"
    assert not (root / "opt/orchestrator/current/MARK").exists()
    done = run_script("rollback.sh", "--destdir", root.as_posix())
    assert done.returncode == 0, done.stderr
    assert (root / "opt/orchestrator/current/MARK").read_text(encoding="utf-8") == "first"
    assert not (root / "opt/orchestrator/previous/MARK").exists()
    run_script("rollback.sh", "--destdir", root.as_posix())  # and back again
    assert (root / "opt/orchestrator/previous/MARK").exists()


def test_a_third_update_replaces_the_previous_one_so_the_card_does_not_fill_up(
    tmp_path: Path,
) -> None:
    for _ in range(3):
        assert install(tmp_path).returncode == 0
    assert sorted(p.name for p in (staged(tmp_path) / "opt/orchestrator").iterdir()) == [
        "current",
        "previous",
    ]


def test_rollback_without_a_previous_program_says_so(tmp_path: Path) -> None:
    assert install(tmp_path).returncode == 0
    done = run_script("rollback.sh", "--destdir", staged(tmp_path).as_posix())
    assert done.returncode != 0 and "no previous program" in done.stderr
    assert (staged(tmp_path) / "opt/orchestrator/current").is_dir()  # nothing was disturbed


def test_the_journal_setting_is_only_written_when_asked_for(tmp_path: Path) -> None:
    assert install(tmp_path).returncode == 0
    drop_in = staged(tmp_path) / "etc/systemd/journald.conf.d/orchestrator.conf"
    assert not drop_in.exists()
    assert install(tmp_path, "--volatile-journal").returncode == 0
    assert "Storage=volatile" in drop_in.read_text(encoding="utf-8")


# --- the logins of the old scripts ----------------------------------------------------------------

MAIL_SECRET = "OLD-MAIL-REFRESH-SENTINEL-1"
CALENDAR_SECRET = "OLD-CALENDAR-REFRESH-SENTINEL-2"
CLIENT_SECRET = "OLD-CLIENT-SECRET-SENTINEL-3"


def old_token(refresh: str | None, scope: str) -> dict[str, object]:
    """The shape of a file written by oauth2client.file.Storage."""
    return {
        "_class": "OAuth2Credentials",
        "_module": "oauth2client.client",
        "access_token": "ya29.old-access",
        "client_id": "123.apps.googleusercontent.com",
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh,
        "scopes": [scope],
        "invalid": False,
        "token_expiry": "2026-10-25T15:07:09Z",
        "token_uri": "https://accounts.google.com/o/oauth2/token",
    }


def old_logins(tmp_path: Path, *, mail: bool = True, calendar: bool = True) -> Path:
    source = tmp_path / "old"
    (source / ".credentials").mkdir(parents=True)
    (source / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": "123", "client_secret": CLIENT_SECRET}}),
        encoding="utf-8",
    )
    if mail:
        (source / ".credentials" / "Gmail-python-quickstart.json").write_text(
            json.dumps(old_token(MAIL_SECRET, GMAIL_SCOPE)), encoding="utf-8"
        )
    if calendar:
        (source / ".credentials" / "Calendar-python-quickstart.json").write_text(
            json.dumps(old_token(CALENDAR_SECRET, CALENDAR_READONLY_SCOPE)), encoding="utf-8"
        )
    return source


def import_install(tmp_path: Path, source: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return run_script(
        "install.sh",
        "--destdir",
        staged(tmp_path).as_posix(),
        "--no-venv",
        "--no-start",
        "--google-from",
        source.as_posix(),
        *extra,
        env={"IMPORT_PYTHON": Path(sys.executable).as_posix()},
    )


def google_dir(tmp_path: Path) -> Path:
    return staged(tmp_path) / "var/lib/orchestrator/google"


def test_the_old_logins_are_converted_and_installed(tmp_path: Path) -> None:
    done = import_install(tmp_path, old_logins(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    folder = google_dir(tmp_path)
    assert sorted(p.name for p in folder.iterdir() if p.is_file()) == [
        "calendar_token.json",
        "client_secret.json",
        "token.json",
    ]
    mail, calendar = read_token_data(folder / "token.json"), read_token_data(
        folder / "calendar_token.json"
    )
    assert (mail["type"], mail["scopes"], mail["refresh_token"]) == (
        "authorized_user",
        [GMAIL_SCOPE],
        MAIL_SECRET,
    )
    assert calendar["scopes"] == [CALENDAR_READONLY_SCOPE]
    assert calendar["token_uri"] == "https://oauth2.googleapis.com/token"
    assert "access_token" not in mail and "_class" not in calendar  # the old fields are gone
    assert "converted Gmail-python-quickstart.json for the mail" in done.stdout


def test_each_converted_login_is_good_for_its_own_job_only(tmp_path: Path) -> None:
    assert import_install(tmp_path, old_logins(tmp_path)).returncode == 0
    folder = google_dir(tmp_path)
    assert status_for(folder / "token.json", "mail").state is TokenState.OK
    assert status_for(folder / "calendar_token.json", "calendar").state is TokenState.OK
    # neither alone has both permissions, which is why the program uses the two files separately
    assert status_for(folder / "token.json", "both").state is TokenState.SCOPES_MISSING
    assert status_for(folder / "token.json", "calendar").state is TokenState.SCOPES_MISSING


def test_no_secret_is_printed_by_the_install(tmp_path: Path) -> None:
    done = import_install(tmp_path, old_logins(tmp_path))
    for secret in (MAIL_SECRET, CALENDAR_SECRET, CLIENT_SECRET):
        assert secret not in done.stdout + done.stderr


def test_a_second_install_never_overwrites_what_is_there(tmp_path: Path) -> None:
    source = old_logins(tmp_path)
    assert import_install(tmp_path, source).returncode == 0
    folder = google_dir(tmp_path)
    (folder / "token.json").write_text("new login", encoding="utf-8")
    (folder / "client_secret.json").write_text("new client", encoding="utf-8")
    done = import_install(tmp_path, source)
    assert (folder / "token.json").read_text(encoding="utf-8") == "new login"
    assert (folder / "client_secret.json").read_text(encoding="utf-8") == "new client"
    assert "token.json exists already, not touched" in done.stdout


def test_missing_old_files_are_skipped_with_a_message(tmp_path: Path) -> None:
    source = old_logins(tmp_path, mail=False, calendar=False)
    done = import_install(tmp_path, source)
    assert done.returncode == 0
    assert "no old Gmail-python-quickstart.json found" in done.stdout
    assert not (google_dir(tmp_path) / "token.json").exists()
    assert (google_dir(tmp_path) / "client_secret.json").exists()


def test_a_login_that_cannot_be_used_is_reported_and_not_installed(tmp_path: Path) -> None:
    source = old_logins(tmp_path)
    (source / ".credentials" / "Gmail-python-quickstart.json").write_text(
        json.dumps(old_token(None, GMAIL_SCOPE)), encoding="utf-8"
    )
    done = import_install(tmp_path, source)
    assert done.returncode == 0  # the install itself goes on
    assert "WARNING: Gmail-python-quickstart.json could not be used" in done.stdout
    assert not (google_dir(tmp_path) / "token.json").exists()
    assert (google_dir(tmp_path) / "calendar_token.json").exists()


def test_the_import_can_be_switched_off(tmp_path: Path) -> None:
    done = import_install(tmp_path, old_logins(tmp_path), "--no-google-import")
    assert done.returncode == 0
    assert [p for p in google_dir(tmp_path).iterdir() if p.is_file()] == []


def test_the_default_source_folder_is_never_used_by_the_other_install_tests(tmp_path: Path) -> None:
    assert install(tmp_path).returncode == 0
    assert [p for p in google_dir(tmp_path).iterdir() if p.is_file()] == []
