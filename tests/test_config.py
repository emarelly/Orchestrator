"""Configuration loader (CS-8, NFR-6, NFR-8, section 6 limit of 5 showers)."""

import dataclasses
import re
from pathlib import Path

import pytest

from orchestrator.config import (
    HARD_MAX_SHOWERS,
    MAX_LOG_STORAGE_BYTES,
    Config,
    ConfigError,
    load_config,
)

ROOT = Path(__file__).resolve().parent.parent
SENTINEL = "TOP-SECRET-SENTINEL-4711"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_a_file() -> None:
    config = load_config(None, env={})
    assert config.general.dry_run is False
    assert config.limits.max_showers == HARD_MAX_SHOWERS == 5.0
    assert config.boiler.url(config.boiler.status_path) == "http://MARELLYPI3:8000/status/"
    assert config.boiler.manual_upload_path is None  # OQ-16 stays undecided, not invented


def test_example_file_loads_and_equals_the_defaults() -> None:
    assert load_config(ROOT / "config.example.toml", env={}) == load_config(None, env={})


def test_file_values_are_loaded_and_normalized(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
[boiler]
base_url = "http://10.0.0.5:8000"
manual_upload_path = "uploadmanual/"
[limits]
max_showers = 4
[mail]
user_email_list = ["A@Example.com", "b@example.org"]
admin_email = "admin@example.com"
""",
    )
    config = load_config(path, env={})
    assert config.boiler.base_url == "http://10.0.0.5:8000/"
    assert config.boiler.manual_upload_path == "uploadmanual/"
    assert config.limits.max_showers == 4.0
    assert config.mail.user_email_list == ("a@example.com", "b@example.org")


def test_config_is_immutable() -> None:
    config = load_config(None, env={})
    field_name = "dry_run"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(config.general, field_name, True)


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("[limits]\nmax_showers = 6\n", "[limits] max_showers: expected a number between"),
        ("[limits]\nmax_showers = 0\n", "[limits] max_showers: expected a number between"),
        ("[limits]\nmax_showers = true\n", "[limits] max_showers: expected a number"),
        ("[web]\nport = 70000\n", "[web] port: expected a whole number between 1 and 65535"),
        ('[web]\nport = "80"\n', "[web] port: expected a whole number"),
        ('[boiler]\nbase_url = "ftp://x/"\n', "[boiler] base_url: expected a URL"),
        ("[boiler]\nbogus = 1\n", "[boiler] unknown key(s): bogus"),
        ("[nonsense]\na = 1\n", "unknown section(s): nonsense"),
        ('[mail]\nadmin_email = "not-an-address"\n', "[mail] admin_email: expected an e-mail"),
        ("[mqtt]\nenabled = true\n", "[mqtt] host: required when"),
        ("[general]\nlog_dir = 5\n", "[general] log_dir: expected"),
        ("[general\n", "is not valid TOML"),
        ('[logging]\nlevel = "LOUD"\n', "[logging] level: expected one of"),
        ("[logging]\nfile_max_bytes = 5\n", "[logging] file_max_bytes: expected a whole number"),
        ("[logging]\nfile_max_bytes = 10485760\n", "[logging] file sizes: main and audit logs"),
    ],
)
def test_invalid_configuration_names_the_key(tmp_path: Path, toml: str, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(write(tmp_path, toml), env={})


def test_default_log_storage_stays_within_20_mb() -> None:
    logging_config = load_config(None, env={}).logging
    assert logging_config.max_storage_bytes() <= MAX_LOG_STORAGE_BYTES == 20 * 1024 * 1024


def test_calendar_write_back_is_off_unless_switched_on(tmp_path: Path) -> None:
    assert load_config(None, env={}).calendar.write_back is False
    assert load_config(
        write(tmp_path, "[calendar]\nwrite_back = true\n"), env={}
    ).calendar.write_back
    with pytest.raises(ConfigError, match=r"\[calendar\] write_back"):
        load_config(write(tmp_path, '[calendar]\nwrite_back = "yes"\n'), env={})


def test_log_level_is_case_insensitive(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, '[logging]\nlevel = "debug"\n'), env={})
    assert config.logging.level == "DEBUG"


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration file not found"):
        load_config(tmp_path / "missing.toml", env={})


def test_error_messages_never_contain_the_offending_value(tmp_path: Path) -> None:
    path = write(tmp_path, f'[web]\nport = "{SENTINEL}"\n[mail]\nadmin_email = "{SENTINEL}"\n')
    with pytest.raises(ConfigError) as caught:
        load_config(path, env={})
    assert SENTINEL not in str(caught.value)
    path = write(tmp_path, f'[mail]\nadmin_email = "{SENTINEL}"\n')
    with pytest.raises(ConfigError) as caught:
        load_config(path, env={})
    assert SENTINEL not in str(caught.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("off", False),
        ("", False),
    ],
)
def test_dry_run_environment_variable_overrides_the_file(
    tmp_path: Path, value: str, expected: bool
) -> None:
    path = write(tmp_path, f"[general]\ndry_run = {str(not expected).lower()}\n")
    assert load_config(path, env={"DRY_RUN": value}).general.dry_run is expected


def test_invalid_dry_run_value_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(None, env={"DRY_RUN": SENTINEL})
    assert "DRY_RUN" in str(caught.value)
    assert SENTINEL not in str(caught.value)


def test_login_requires_a_password_from_the_environment(tmp_path: Path) -> None:
    path = write(tmp_path, "[web]\nlogin_required = true\n")
    with pytest.raises(ConfigError, match="UI_PASSWORD"):
        load_config(path, env={})
    assert load_config(path, env={"UI_PASSWORD": SENTINEL}).secrets.ui_password == SENTINEL


def test_secrets_never_appear_in_repr_or_description() -> None:
    env = {
        "BOILER_SSH_PASSWORD": SENTINEL,
        "MQTT_PASSWORD": SENTINEL,
        "UI_PASSWORD": SENTINEL,
        "GOOGLE_TOKEN_FILE": SENTINEL,
    }
    config: Config = load_config(None, env=env)
    assert SENTINEL not in repr(config)
    assert SENTINEL not in str(config.describe())
    assert config.describe()["secrets"]["ssh_password"] == "***"
    assert config.describe()["secrets"]["ssh_user"] == "(not set)"


def test_security_alert_limit_has_a_default_and_a_range(tmp_path: Path) -> None:
    assert load_config(None, env={}).mail.alert_daily_max == 20
    assert (
        load_config(write(tmp_path, "[mail]\nalert_daily_max = 5\n"), env={}).mail.alert_daily_max
        == 5
    )
    for bad in ("0", "1001", '"many"'):
        with pytest.raises(ConfigError, match=r"\[mail\] alert_daily_max"):
            load_config(write(tmp_path, f"[mail]\nalert_daily_max = {bad}\n"), env={})


def test_ssh_settings_have_defaults_a_range_and_a_checked_fingerprint(tmp_path: Path) -> None:
    boiler = load_config(None, env={}).boiler
    assert (boiler.ssh_port, boiler.ssh_host_key, boiler.reboot_grace_s) == (22, "", 300.0)
    good = "SHA256:" + "A" * 43
    config = load_config(
        write(
            tmp_path, f'[boiler]\nssh_port = 2222\nssh_host_key = "{good}"\nreboot_grace_s = 0\n'
        ),
        env={},
    )
    assert (config.boiler.ssh_port, config.boiler.ssh_host_key) == (2222, good)
    for bad in ('"abc"', '"SHA256:short"', '"MD5:' + "a" * 43 + '"'):
        with pytest.raises(ConfigError, match=r"\[boiler\] ssh_host_key"):
            load_config(write(tmp_path, f"[boiler]\nssh_host_key = {bad}\n"), env={})
    with pytest.raises(ConfigError, match=r"\[boiler\] ssh_port"):
        load_config(write(tmp_path, "[boiler]\nssh_port = 70000\n"), env={})


def test_mqtt_settings_have_defaults_and_are_checked(tmp_path: Path) -> None:
    mqtt = load_config(None, env={}).mqtt
    assert (mqtt.tls, mqtt.client_id, mqtt.discovery_prefix) == (
        False,
        "orchestrator",
        "homeassistant",
    )
    assert (mqtt.keepalive_s, mqtt.state_interval_s, mqtt.relay_on_seconds) == (60, 60, 600)
    for bad in (
        'base_topic = "has space"',
        'base_topic = "a//b"',
        'base_topic = "#"',
        'discovery_prefix = "/x"',
        'client_id = "a/b"',
        "state_interval_s = 120",
        "relay_on_seconds = 5",
        "keepalive_s = 1",
        'tls = "yes"',
    ):
        with pytest.raises(ConfigError, match=r"\[mqtt\]"):
            load_config(write(tmp_path, f"[mqtt]\n{bad}\n"), env={})
    ok = load_config(write(tmp_path, '[mqtt]\nbase_topic = "home/boiler"\ntls = true\n'), env={})
    assert ok.mqtt.base_topic == "home/boiler" and ok.mqtt.tls is True


def test_the_mail_check_interval_has_a_default_and_a_range(tmp_path: Path) -> None:
    assert load_config(None, env={}).mail.check_interval_s == 30.0
    for bad in ("1", "5000", '"often"'):
        with pytest.raises(ConfigError, match=r"\[mail\] check_interval_s"):
            load_config(write(tmp_path, f"[mail]\ncheck_interval_s = {bad}\n"), env={})


def test_the_calendar_polling_interval_has_a_default_and_a_range(tmp_path: Path) -> None:
    assert load_config(None, env={}).calendar.poll_interval_s == 300.0
    assert (
        load_config(
            write(tmp_path, "[calendar]\npoll_interval_s = 60\n"), env={}
        ).calendar.poll_interval_s
        == 60.0
    )
    for bad in ("1", "100000", '"often"'):
        with pytest.raises(ConfigError, match=r"\[calendar\] poll_interval_s"):
            load_config(write(tmp_path, f"[calendar]\npoll_interval_s = {bad}\n"), env={})
