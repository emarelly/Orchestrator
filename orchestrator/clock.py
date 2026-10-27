"""Clock and time zone (CS-6, CS-13, NFR-4).

All code asks a :class:`Clock` for the time instead of calling ``datetime.now()`` itself, so
tests can freeze and move time. The clock returns timezone-aware datetimes in one configured zone.

The zone is, in this order: the ``[general] timezone`` setting, the ``TZ`` environment variable,
``/etc/timezone``, the target of the ``/etc/localtime`` link, and finally the operating system's
current offset (a fixed offset, so daylight-saving changes are not followed: only the Windows
development machine gets there; the Pi always has a named zone).
"""

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orchestrator.config import Config, ConfigError


class Clock(Protocol):
    """What the rest of the code may ask about time."""

    @property
    def tz(self) -> tzinfo:
        """The configured time zone."""
        ...

    def now(self) -> datetime:
        """Return the current time as a timezone-aware datetime in the configured zone."""
        ...

    def monotonic(self) -> float:
        """Return seconds from a clock that never goes backwards (for timeouts and intervals)."""
        ...


class SystemClock:
    """The real clock."""

    def __init__(self, tz: tzinfo) -> None:
        self._tz = tz

    @property
    def tz(self) -> tzinfo:
        return self._tz

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True, slots=True)
class ResolvedTimezone:
    """The chosen zone and where it came from (logged at start-up)."""

    tz: tzinfo
    name: str
    source: str


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _zone_from_localtime(link: Path) -> ZoneInfo | None:
    target = os.path.realpath(link).replace("\\", "/")
    marker = "zoneinfo/"
    if marker not in target:
        return None
    return _zone(target.split(marker, 1)[1])


def resolve_timezone(
    configured: str = "",
    *,
    env: Mapping[str, str] | None = None,
    timezone_file: Path = Path("/etc/timezone"),
    localtime_link: Path = Path("/etc/localtime"),
) -> ResolvedTimezone:
    """Pick the time zone as described in the module docstring.

    Raises :class:`ConfigError` only if the *configured* name is unknown; the other sources are
    skipped when unusable.
    """
    environment = os.environ if env is None else env
    if configured:
        zone = _zone(configured)
        if zone is None:
            raise ConfigError("[general] timezone: unknown time zone name")
        return ResolvedTimezone(zone, configured, "configuration")

    from_env = environment.get("TZ", "").lstrip(":")
    if from_env and (zone := _zone(from_env)) is not None:
        return ResolvedTimezone(zone, from_env, "TZ environment variable")

    try:
        from_file = timezone_file.read_text(encoding="utf-8").strip()
    except OSError:
        from_file = ""
    if from_file and (zone := _zone(from_file)) is not None:
        return ResolvedTimezone(zone, from_file, str(timezone_file))

    if (zone := _zone_from_localtime(localtime_link)) is not None:
        return ResolvedTimezone(zone, zone.key, str(localtime_link))

    local = datetime.now().astimezone().tzinfo or UTC
    return ResolvedTimezone(local, str(local), "operating system offset (no DST rules)")


def system_clock(config: Config, *, env: Mapping[str, str] | None = None) -> SystemClock:
    """Build the real clock for the configured (or detected) zone."""
    return SystemClock(resolve_timezone(config.general.timezone, env=env).tz)
