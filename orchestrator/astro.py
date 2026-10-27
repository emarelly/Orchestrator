"""Sunrise, sunset and moon times for calendar timer events (FR-CFG-6a).

Computed with the ``astral`` library for a location and a named time zone, **for the date of the
event**: the zone's daylight-saving rules of that date apply, so a sunset in the week the clocks
change comes out right (the legacy code added the *current* UTC offset). Times are cut to whole
seconds, as the timer service expects.

Names (see :mod:`orchestrator.event_titles`): ``sunrise sunset dawn dusk noon midnight moonrise
moonset twilight``. ``dawn`` and ``dusk`` are civil dawn / dusk, ``twilight`` means civil dusk and
``midnight`` the solar midnight that falls on the date (shortly after 00:00 in summer time).

The location comes from the configuration or from a small cache file written once by a setup step
(geocoding needs the internet and a library that is not installed on the Pi, so it never runs in the
service). Without a location, events that need the sun or moon cannot be computed: the caller gets
an :class:`AstroError` and skips and reports that event instead of failing (CS-11).
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path

from astral import Observer
from astral.moon import moonrise, moonset
from astral.sun import midnight, sun

from orchestrator.config import Config
from orchestrator.event_titles import AstroTime
from orchestrator.files import write_atomic
from orchestrator.logging_setup import kv

logger = logging.getLogger(__name__)

LOCATION_CACHE_FILENAME = "location.json"
_SUN_KEYS = {
    "sunrise": "sunrise",
    "sunset": "sunset",
    "dawn": "dawn",
    "dusk": "dusk",
    "twilight": "dusk",  # civil dusk
    "noon": "noon",
}


class AstroError(ValueError):
    """A sun or moon time cannot be computed (no location, unknown name, no such event that day)."""


@dataclass(frozen=True, slots=True)
class Location:
    """Where the boiler is, in degrees (north and east are positive)."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0 or not -180.0 <= self.longitude <= 180.0:
            raise AstroError("latitude must be -90..90 and longitude -180..180")


def resolve_astro(astro: AstroTime, day: date, location: Location | None, tz: tzinfo) -> datetime:
    """Return the moment of ``astro`` on ``day`` in zone ``tz`` (offset in minutes included).

    The result is timezone-aware and may fall on the previous or the next date once the offset is
    applied. Raises :class:`AstroError` if it cannot be computed.
    """
    if location is None:
        raise AstroError(
            f"{astro.name}: no location is configured (set [location] latitude and longitude)"
        )
    observer = Observer(latitude=location.latitude, longitude=location.longitude)
    moment: datetime | None
    try:
        if astro.name in _SUN_KEYS:
            moment = sun(observer, date=day, tzinfo=tz)[_SUN_KEYS[astro.name]]
        elif astro.name == "midnight":
            moment = midnight(observer, date=day, tzinfo=tz)
        elif astro.name == "moonrise":
            moment = moonrise(observer, date=day, tzinfo=tz)
        elif astro.name == "moonset":
            moment = moonset(observer, date=day, tzinfo=tz)
        else:
            raise AstroError(f"{astro.name!r} is not a known sun or moon time")
    except AstroError:
        raise
    except (ValueError, TypeError, ArithmeticError):
        raise AstroError(_no_event(astro, day)) from None
    if moment is None:  # astral returns None for a moon event that does not happen that day
        raise AstroError(_no_event(astro, day))
    # add the offset on the absolute time line, so it is right even across a clock change
    shifted = (moment.astimezone(UTC) + timedelta(minutes=astro.offset_minutes)).astimezone(tz)
    return shifted.replace(microsecond=0)


def _no_event(astro: AstroTime, day: date) -> str:
    return f"{astro.name} does not occur on {day.isoformat()} at this location"


# --- the location -------------------------------------------------------------------------------


def load_location(config: Config) -> Location | None:
    """Return the configured location, else the cached one, else ``None``."""
    configured = config.location
    if configured.latitude is not None and configured.longitude is not None:
        return Location(configured.latitude, configured.longitude)
    path = Path(config.general.data_dir) / LOCATION_CACHE_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Location(float(raw["latitude"]), float(raw["longitude"]))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AstroError):
        logger.warning(kv("location_cache_unreadable", file=path.name))
        return None


def save_location(directory: Path, location: Location) -> bool:
    """Write the location cache (used by the one-time setup step); False if it was unchanged."""
    text = json.dumps({"latitude": location.latitude, "longitude": location.longitude}, indent=2)
    return write_atomic(
        directory / LOCATION_CACHE_FILENAME, text.encode("utf-8"), keep_backup=False
    )
