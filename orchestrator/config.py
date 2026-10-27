"""Configuration loading (CS-8, NFR-6, NFR-8).

Settings come from one TOML file; secrets come only from environment variables. The result is a
validated, immutable :class:`Config`. Any problem raises :class:`ConfigError` whose message names
the key or variable but never its value, so a bad secret cannot leak into a log or a terminal.

Values that are still undecided (the manual-file upload URLs, OQ-16) are optional keys that default
to "not configured" instead of being invented.
"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orchestrator.models import ModelError
from orchestrator.relays import DEFAULT_RELAY_NAME_PAIRS, RelayNames

HARD_MAX_SHOWERS = 5.0
"""The heater limit (Requirements section 6): no configuration may raise the cap above this."""

ENV_DRY_RUN = "DRY_RUN"
SECRET_VARIABLES = (
    "BOILER_SSH_USER",
    "BOILER_SSH_PASSWORD",
    "BOILER_SSH_KEY_FILE",
    "MQTT_PASSWORD",
    "UI_PASSWORD",
    "GOOGLE_CLIENT_SECRET_FILE",
    "GOOGLE_TOKEN_FILE",
    "GOOGLE_CALENDAR_TOKEN_FILE",
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """The configuration is missing, malformed or inconsistent (message never contains values)."""


@dataclass(frozen=True, slots=True)
class GeneralConfig:
    dry_run: bool = False
    timezone: str = ""  # IANA name such as Asia/Jerusalem; empty = use the operating system
    data_dir: str = "data"
    log_dir: str = "logs"


@dataclass(frozen=True, slots=True)
class BoilerConfig:
    """The boiler Pi REST API (Requirements section 3.6); paths are relative to ``base_url``."""

    base_url: str = "http://MARELLYPI3:8000/"
    request_timeout_s: float = 5.0
    poll_interval_s: float = 10.0
    down_after_failures: int = 3  # failed polls in a row before the Pi counts as down
    stale_after_s: float = 300.0  # a reading older than this is shown as stale
    status_path: str = "status/"
    history_path: str = "history/"
    log_path: str = "log/"
    relay_status_path: str = "relay/?relay="
    update_schedule_path: str = "updatecalander/"
    update_timer_path: str = "updaterelaycalander/"
    relay_on_path: str = "relayon/"
    relay_off_path: str = "relayoff/"
    manual_upload_path: str | None = None  # OQ-16: not decided yet
    relay_manual_upload_path: str | None = None  # OQ-16: not decided yet
    ssh_port: int = 22  # for the reboot (FR-LEG-3a); the user and key / password are secrets
    ssh_host_key: str = ""  # SHA256 fingerprint of the boiler Pi's host key; empty = no reboot
    ssh_timeout_s: float = 10.0
    reboot_grace_s: float = 300.0  # down / up alerts are held back this long after a reboot

    def url(self, path: str) -> str:
        """Return the full URL of an endpoint path."""
        return self.base_url + path


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    """Manual-control limits (FR-MAN-2)."""

    max_showers: float = HARD_MAX_SHOWERS
    manual_margin: float = 1.0
    manual_duration_hours: float = 3.0


@dataclass(frozen=True, slots=True)
class WebConfig:
    host: str = (
        "0.0.0.0"  # listens on the LAN; the service is never exposed to the internet (NFR-8)
    )
    port: int = 8080
    login_required: bool = False


@dataclass(frozen=True, slots=True)
class MqttConfig:
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    base_topic: str = "orchestrator"
    tls: bool = False
    client_id: str = "orchestrator"
    discovery_prefix: str = "homeassistant"  # Home Assistant's MQTT discovery prefix
    keepalive_s: int = 60  # the broker publishes the last will after about 1.5 x this
    state_interval_s: int = 60  # the state is also republished this often (FR-MQTT-3)
    relay_on_seconds: int = 600  # how long a relay switch turned ON in Home Assistant runs


@dataclass(frozen=True, slots=True)
class MailConfig:
    user_email_list: tuple[str, ...] = ()
    admin_email: str = ""
    security_alert_email: str = ""
    alert_daily_max: int = 20  # security alert e-mails per day at most (FR-LEG-3b)
    check_interval_s: float = 30.0  # how often the inbox is read for commands


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    calendar_id: str = "primary"
    poll_interval_s: float = 300.0
    write_back: bool = False  # UI edits change the real calendar only when this is switched on


@dataclass(frozen=True, slots=True)
class LocationConfig:
    """Where the boiler is, for sunrise / sunset times; empty means look in the location cache."""

    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class RelaysConfig:
    """Relay names usable in calendar titles (Requirements section 3.9); pairs keep their order."""

    names: tuple[tuple[str, int], ...] = DEFAULT_RELAY_NAME_PAIRS


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MAX_LOG_STORAGE_BYTES = 20 * 1024 * 1024
"""Main log plus audit log together may never need more than this on the SD card (LOG-12)."""


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Logging settings (LOG-2, LOG-3, LOG-5, LOG-8, LOG-12)."""

    level: str = "INFO"
    json_format: bool = False
    console: bool = True
    file_max_bytes: int = 2 * 1024 * 1024
    file_backup_count: int = 3
    audit_max_bytes: int = 1024 * 1024
    audit_backup_count: int = 5
    ring_size: int = 200
    collapse_window_s: float = 600.0

    def max_storage_bytes(self) -> int:
        """Worst-case disk use of the main and audit logs, including all rotated files."""
        main = self.file_max_bytes * (self.file_backup_count + 1)
        audit = self.audit_max_bytes * (self.audit_backup_count + 1)
        return main + audit


@dataclass(frozen=True, slots=True)
class Secrets:
    """Values read from environment variables; never printed (``repr`` is hidden)."""

    ssh_user: str | None = field(default=None, repr=False)
    ssh_password: str | None = field(default=None, repr=False)
    ssh_key_file: str | None = field(default=None, repr=False)
    mqtt_password: str | None = field(default=None, repr=False)
    ui_password: str | None = field(default=None, repr=False)
    google_client_secret_file: str | None = field(default=None, repr=False)
    google_token_file: str | None = field(default=None, repr=False)
    google_calendar_token_file: str | None = field(default=None, repr=False)

    def password_values(self) -> tuple[str, ...]:
        """Return the values that must never reach a log (passwords; file paths are not secret)."""
        values = (self.ssh_password, self.mqtt_password, self.ui_password)
        return tuple(value for value in values if value)


@dataclass(frozen=True, slots=True)
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    boiler: BoilerConfig = field(default_factory=BoilerConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    relays: RelaysConfig = field(default_factory=RelaysConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    secrets: Secrets = field(default_factory=Secrets)

    def describe(self) -> dict[str, Any]:
        """Return a log-safe summary: settings as they are, secrets only as set / not set."""
        summary: dict[str, Any] = {
            "general": self.general,
            "boiler": self.boiler,
            "limits": self.limits,
            "web": self.web,
            "mqtt": self.mqtt,
            "mail": self.mail,
            "calendar": self.calendar,
            "logging": self.logging,
            "relays": self.relays,
            "location": self.location,
        }
        summary["secrets"] = {
            name: "***" if getattr(self.secrets, name) else "(not set)"
            for name in Secrets.__slots__
        }
        return summary


class _Section:
    """Reads and type-checks the keys of one TOML table, then rejects unknown keys."""

    def __init__(self, name: str, data: Mapping[str, Any]) -> None:
        self._name = name
        self._data = dict(data)
        self._seen: set[str] = set()

    def _fail(self, key: str, expected: str) -> ConfigError:
        return ConfigError(f"[{self._name}] {key}: expected {expected}")

    def _get(self, key: str) -> Any:
        self._seen.add(key)
        return self._data.get(key)

    def text(self, key: str, default: str, *, allow_empty: bool = False) -> str:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, str) or (not value and not allow_empty):
            raise self._fail(key, "a non-empty text" if not allow_empty else "text")
        return value

    def choice(self, key: str, default: str, allowed: tuple[str, ...]) -> str:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, str) or value.upper() not in allowed:
            raise self._fail(key, f"one of {', '.join(allowed)}")
        return value.upper()

    def name_number_pairs(
        self, key: str, default: tuple[tuple[str, int], ...]
    ) -> tuple[tuple[str, int], ...]:
        value = self._get(key)
        if value is None:
            return default
        pairs: list[tuple[str, int]] = []
        if not isinstance(value, list):
            raise self._fail(key, "a list of [name, relay number] pairs")
        for item in value:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
            ):
                raise self._fail(key, "a list of [name, relay number] pairs")
            pairs.append((item[0], item[1]))
        try:
            RelayNames(pairs)
        except ModelError as error:
            raise self._fail(key, str(error)) from None
        return tuple(pairs)

    def optional_text(self, key: str) -> str | None:
        value = self._get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise self._fail(key, "a non-empty text")
        return value

    def number(
        self, key: str, default: float, *, minimum: float, maximum: float | None = None
    ) -> float:
        value = self._get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self._fail(key, "a number")
        too_big = maximum is not None and value > maximum
        if value < minimum or too_big:
            limit = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
            raise self._fail(key, f"a number {limit}")
        return float(value)

    def optional_number(self, key: str, *, minimum: float, maximum: float) -> float | None:
        value = self._get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self._fail(key, "a number")
        if not minimum <= value <= maximum:
            raise self._fail(key, f"a number between {minimum} and {maximum}")
        return float(value)

    def integer(self, key: str, default: int, *, minimum: int, maximum: int) -> int:
        value = self._get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._fail(key, "a whole number")
        if not minimum <= value <= maximum:
            raise self._fail(key, f"a whole number between {minimum} and {maximum}")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise self._fail(key, "true or false")
        return value

    def email(self, key: str) -> str:
        value = self.text(key, "", allow_empty=True)
        if value and not _is_email(value):
            raise self._fail(key, "an e-mail address")
        return value

    def email_list(self, key: str) -> tuple[str, ...]:
        value = self._get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(
            isinstance(item, str) and _is_email(item) for item in value
        ):
            raise self._fail(key, "a list of e-mail addresses")
        return tuple(item.strip().lower() for item in value)

    def finish(self) -> None:
        unknown = sorted(set(self._data) - self._seen)
        if unknown:
            raise ConfigError(f"[{self._name}] unknown key(s): {', '.join(unknown)}")


def _is_email(text: str) -> bool:
    local, _, domain = text.strip().partition("@")
    return bool(local) and "." in domain and " " not in text.strip() and "@" not in domain


def _parse_dry_run(env: Mapping[str, str], default: bool) -> bool:
    raw = env.get(ENV_DRY_RUN)
    if raw is None:
        return default
    word = raw.strip().lower()
    if word in _TRUE:
        return True
    if word in _FALSE:
        return False
    raise ConfigError(
        f"environment variable {ENV_DRY_RUN}: expected 1/0, true/false, yes/no or on/off"
    )


def _read_secrets(env: Mapping[str, str]) -> Secrets:
    def pick(name: str) -> str | None:
        return env.get(name) or None

    return Secrets(
        ssh_user=pick("BOILER_SSH_USER"),
        ssh_password=pick("BOILER_SSH_PASSWORD"),
        ssh_key_file=pick("BOILER_SSH_KEY_FILE"),
        mqtt_password=pick("MQTT_PASSWORD"),
        ui_password=pick("UI_PASSWORD"),
        google_client_secret_file=pick("GOOGLE_CLIENT_SECRET_FILE"),
        google_token_file=pick("GOOGLE_TOKEN_FILE"),
        google_calendar_token_file=pick("GOOGLE_CALENDAR_TOKEN_FILE"),
    )


def _check_timezone(name: str) -> str:
    if name:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ConfigError("[general] timezone: unknown time zone name") from None
    return name


def _table(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}]: expected a table")
    return value


def _build(raw: Mapping[str, Any], env: Mapping[str, str]) -> Config:
    sections = {
        "general",
        "boiler",
        "limits",
        "web",
        "mqtt",
        "mail",
        "calendar",
        "logging",
        "relays",
        "location",
    }
    unknown = sorted(set(raw) - sections)
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")

    sec = _Section("general", _table(raw, "general"))
    general = GeneralConfig(
        dry_run=_parse_dry_run(env, sec.boolean("dry_run", False)),
        timezone=_check_timezone(sec.text("timezone", "", allow_empty=True)),
        data_dir=sec.text("data_dir", "data"),
        log_dir=sec.text("log_dir", "logs"),
    )
    sec.finish()

    sec = _Section("boiler", _table(raw, "boiler"))
    defaults = BoilerConfig()
    base_url = sec.text("base_url", defaults.base_url)
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("[boiler] base_url: expected a URL starting with http:// or https://")
    boiler = BoilerConfig(
        base_url=base_url if base_url.endswith("/") else base_url + "/",
        request_timeout_s=sec.number("request_timeout_s", 5.0, minimum=0.5, maximum=60.0),
        poll_interval_s=sec.number("poll_interval_s", 10.0, minimum=1.0, maximum=3600.0),
        down_after_failures=sec.integer(
            "down_after_failures", defaults.down_after_failures, minimum=1, maximum=100
        ),
        stale_after_s=sec.number(
            "stale_after_s", defaults.stale_after_s, minimum=30.0, maximum=86400.0
        ),
        status_path=sec.text("status_path", defaults.status_path),
        history_path=sec.text("history_path", defaults.history_path),
        log_path=sec.text("log_path", defaults.log_path),
        relay_status_path=sec.text("relay_status_path", defaults.relay_status_path),
        update_schedule_path=sec.text("update_schedule_path", defaults.update_schedule_path),
        update_timer_path=sec.text("update_timer_path", defaults.update_timer_path),
        relay_on_path=sec.text("relay_on_path", defaults.relay_on_path),
        relay_off_path=sec.text("relay_off_path", defaults.relay_off_path),
        manual_upload_path=sec.optional_text("manual_upload_path"),
        relay_manual_upload_path=sec.optional_text("relay_manual_upload_path"),
        ssh_port=sec.integer("ssh_port", defaults.ssh_port, minimum=1, maximum=65535),
        ssh_host_key=sec.text("ssh_host_key", defaults.ssh_host_key, allow_empty=True),
        ssh_timeout_s=sec.number(
            "ssh_timeout_s", defaults.ssh_timeout_s, minimum=1.0, maximum=120.0
        ),
        reboot_grace_s=sec.number(
            "reboot_grace_s", defaults.reboot_grace_s, minimum=0.0, maximum=3600.0
        ),
    )
    if boiler.ssh_host_key and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", boiler.ssh_host_key):
        raise ConfigError(
            "[boiler] ssh_host_key: expected a fingerprint like SHA256:<43 characters> "
            "(run python -m orchestrator.ssh_reboot --show-host-key)"
        )
    sec.finish()

    sec = _Section("limits", _table(raw, "limits"))
    limits = LimitsConfig(
        max_showers=sec.number(
            "max_showers", HARD_MAX_SHOWERS, minimum=0.1, maximum=HARD_MAX_SHOWERS
        ),
        manual_margin=sec.number("manual_margin", 1.0, minimum=0.0, maximum=HARD_MAX_SHOWERS),
        manual_duration_hours=sec.number("manual_duration_hours", 3.0, minimum=0.1, maximum=24.0),
    )
    sec.finish()

    sec = _Section("web", _table(raw, "web"))
    web = WebConfig(
        host=sec.text("host", WebConfig().host),
        port=sec.integer("port", 8080, minimum=1, maximum=65535),
        login_required=sec.boolean("login_required", False),
    )
    sec.finish()

    sec = _Section("mqtt", _table(raw, "mqtt"))
    mqtt = MqttConfig(
        enabled=sec.boolean("enabled", False),
        host=sec.text("host", "", allow_empty=True),
        port=sec.integer("port", 1883, minimum=1, maximum=65535),
        username=sec.text("username", "", allow_empty=True),
        base_topic=sec.text("base_topic", "orchestrator"),
        tls=sec.boolean("tls", False),
        client_id=sec.text("client_id", "orchestrator"),
        discovery_prefix=sec.text("discovery_prefix", "homeassistant"),
        keepalive_s=sec.integer("keepalive_s", 60, minimum=10, maximum=600),
        state_interval_s=sec.integer("state_interval_s", 60, minimum=5, maximum=60),
        relay_on_seconds=sec.integer("relay_on_seconds", 600, minimum=11, maximum=86400),
    )
    for key, value in (
        ("base_topic", mqtt.base_topic),
        ("discovery_prefix", mqtt.discovery_prefix),
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*", value):
            raise ConfigError(f"[mqtt] {key}: expected letters, digits, _ - and / only")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", mqtt.client_id):
        raise ConfigError("[mqtt] client_id: expected 1 to 64 letters, digits, _ or -")
    sec.finish()

    sec = _Section("mail", _table(raw, "mail"))
    mail = MailConfig(
        user_email_list=sec.email_list("user_email_list"),
        admin_email=sec.email("admin_email"),
        security_alert_email=sec.email("security_alert_email"),
        alert_daily_max=sec.integer("alert_daily_max", 20, minimum=1, maximum=1000),
        check_interval_s=sec.number("check_interval_s", 30.0, minimum=5.0, maximum=3600.0),
    )
    sec.finish()

    sec = _Section("calendar", _table(raw, "calendar"))
    calendar = CalendarConfig(
        calendar_id=sec.text("calendar_id", "primary"),
        poll_interval_s=sec.number("poll_interval_s", 300.0, minimum=30.0, maximum=86400.0),
        write_back=sec.boolean("write_back", False),
    )
    sec.finish()

    sec = _Section("logging", _table(raw, "logging"))
    log_defaults = LoggingConfig()
    logging_config = LoggingConfig(
        level=sec.choice("level", log_defaults.level, LOG_LEVELS),
        json_format=sec.boolean("json_format", log_defaults.json_format),
        console=sec.boolean("console", log_defaults.console),
        file_max_bytes=sec.integer(
            "file_max_bytes", log_defaults.file_max_bytes, minimum=10_000, maximum=20 * 1024 * 1024
        ),
        file_backup_count=sec.integer(
            "file_backup_count", log_defaults.file_backup_count, minimum=0, maximum=20
        ),
        audit_max_bytes=sec.integer(
            "audit_max_bytes",
            log_defaults.audit_max_bytes,
            minimum=10_000,
            maximum=20 * 1024 * 1024,
        ),
        audit_backup_count=sec.integer(
            "audit_backup_count", log_defaults.audit_backup_count, minimum=0, maximum=20
        ),
        ring_size=sec.integer("ring_size", log_defaults.ring_size, minimum=10, maximum=5000),
        collapse_window_s=sec.number(
            "collapse_window_s", log_defaults.collapse_window_s, minimum=0.0, maximum=86400.0
        ),
    )
    sec.finish()
    if logging_config.max_storage_bytes() > MAX_LOG_STORAGE_BYTES:
        raise ConfigError("[logging] file sizes: main and audit logs together would exceed 20 MB")

    sec = _Section("relays", _table(raw, "relays"))
    relays = RelaysConfig(names=sec.name_number_pairs("names", DEFAULT_RELAY_NAME_PAIRS))
    sec.finish()

    sec = _Section("location", _table(raw, "location"))
    location = LocationConfig(
        latitude=sec.optional_number("latitude", minimum=-90.0, maximum=90.0),
        longitude=sec.optional_number("longitude", minimum=-180.0, maximum=180.0),
    )
    sec.finish()
    if (location.latitude is None) != (location.longitude is None):
        raise ConfigError("[location] latitude and longitude must be given together")

    secrets = _read_secrets(env)
    if mqtt.enabled and not mqtt.host:
        raise ConfigError("[mqtt] host: required when [mqtt] enabled = true")
    if web.login_required and not secrets.ui_password:
        raise ConfigError(
            "environment variable UI_PASSWORD: required when [web] login_required = true"
        )

    return Config(
        general=general,
        boiler=boiler,
        limits=limits,
        web=web,
        mqtt=mqtt,
        mail=mail,
        calendar=calendar,
        logging=logging_config,
        relays=relays,
        location=location,
        secrets=secrets,
    )


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Load and validate the configuration.

    ``path`` is the TOML file (``None`` means built-in defaults only); ``env`` defaults to
    ``os.environ``. Raises :class:`ConfigError` on any problem.
    """
    environment = os.environ if env is None else env
    raw: dict[str, Any] = {}
    if path is not None:
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except FileNotFoundError:
            raise ConfigError(f"configuration file not found: {path}") from None
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"configuration file {path} is not valid TOML: {error}") from None
    return _build(raw, environment)
