"""Building the manual files (FR-MAN-1..7, FR-LEG-6).

Manual control works by uploading a file to the boiler Pi, built here from the current status and
the clock (no I/O):

* **On**: ``manual.json`` with Target and Min *above the current showers*, never above the heater
  limit, expiring after the configured time (3 h);
* **Off**: ``manual.json`` with Target 0 and Min 0, same expiry;
* **Auto**: the empty ``manual.json`` (``[]``);
* **relay run**: ``<n>Timermanual.json`` with an ON entry now and an OFF entry ``seconds`` later.

The day and time inside ``manual.json`` are the *expiry*, computed by
:func:`orchestrator.weekdays.expiry_after`.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from orchestrator.config import LimitsConfig
from orchestrator.models import (
    BoilerStatus,
    ManualEntry,
    ManualOverride,
    TimerEntry,
    to_decimal,
)
from orchestrator.schedule_files import serialize_manual, serialize_relay_manual
from orchestrator.validation import (
    HARD_MAX_SHOWERS,
    check_manual_entry,
    check_manual_relay,
    check_manual_relay_number,
)
from orchestrator.weekdays import day_and_time, expiry_after

MANUAL_FILENAME = "manual.json"
_TENTH = Decimal("0.1")


class ManualError(ValueError):
    """A manual request cannot be turned into a file (for example the status is unknown)."""


@dataclass(frozen=True, slots=True)
class ManualUpload:
    """A file ready to be sent to the boiler Pi."""

    filename: str
    content: bytes


def relay_filename(relay: int) -> str:
    """Return the name of the manual file of one relay, for example ``2Timermanual.json``."""
    return f"{relay}Timermanual.json"


def _cap(limits: LimitsConfig) -> Decimal:
    return min(to_decimal(limits.max_showers), HARD_MAX_SHOWERS).quantize(
        _TENTH, rounding=ROUND_FLOOR
    )


def manual_on_level(status: BoilerStatus | None, limits: LimitsConfig) -> Decimal:
    """Return the Target / Min for manual On: current showers + margin, rounded up, capped.

    "Current showers" is the larger of the linear and the thermodynamic value. If the boiler is
    already at or above the cap the result is the cap (it cannot be higher), otherwise it is
    always above the current value.
    """
    if status is None:
        raise ManualError("the boiler status is unknown, so manual On cannot choose a level")
    current = max(to_decimal(status.showers_linear), to_decimal(status.showers_thermo))
    if current < 0:
        raise ManualError("the boiler does not report a valid number of showers yet")
    wanted = (current + to_decimal(limits.manual_margin)).quantize(_TENTH, rounding=ROUND_CEILING)
    return min(wanted, _cap(limits))


def _finish(entry: ManualEntry, limits: LimitsConfig) -> ManualOverride:
    check_manual_entry(entry, _cap(limits)).raise_if_errors()
    return ManualOverride(entry)


MIN_MANUAL_HOURS = 0.25
MAX_MANUAL_HOURS = 12.0
"""Longest manual override a channel may ask for; the file holds a weekday and a clock time only."""


def manual_duration(limits: LimitsConfig, hours: float | None) -> timedelta:
    """The length of a manual override: the configured one, or what a channel asked for."""
    if hours is None:
        return timedelta(hours=limits.manual_duration_hours)
    if (
        isinstance(hours, bool)
        or not isinstance(hours, int | float)
        or not MIN_MANUAL_HOURS <= hours <= MAX_MANUAL_HOURS
    ):
        raise ManualError(
            f"the duration must be between {MIN_MANUAL_HOURS * 60:.0f} minutes and "
            f"{MAX_MANUAL_HOURS:.0f} hours"
        )
    return timedelta(hours=hours)


def build_manual_on(
    status: BoilerStatus | None, now: datetime, limits: LimitsConfig, hours: float | None = None
) -> ManualOverride:
    """Build the ``manual.json`` content for manual On."""
    level = manual_on_level(status, limits)
    day, at = expiry_after(now, manual_duration(limits, hours))
    return _finish(ManualEntry(day, at, level, level), limits)


def build_manual_off(
    now: datetime, limits: LimitsConfig, hours: float | None = None
) -> ManualOverride:
    """Build the ``manual.json`` content for manual Off (Target 0, Min 0)."""
    day, at = expiry_after(now, manual_duration(limits, hours))
    return _finish(ManualEntry(day, at, Decimal(0), Decimal(0)), limits)


def build_auto() -> ManualOverride:
    """Build the empty ``manual.json`` content: no override, the schedule applies."""
    return ManualOverride()


def manual_upload(override: ManualOverride) -> ManualUpload:
    """Return the file to upload for a manual override (or for Auto)."""
    return ManualUpload(MANUAL_FILENAME, serialize_manual(override))


def build_relay_run(relay: int, seconds: int, now: datetime) -> tuple[TimerEntry, TimerEntry]:
    """Return the ON entry (now) and the OFF entry (``seconds`` later) of a manual relay run."""
    check_manual_relay(relay, seconds).raise_if_errors()
    off_day, off_at = expiry_after(now, timedelta(seconds=seconds))
    on_day, on_at = day_and_time(now)
    return TimerEntry(on_day, on_at, 1, relay), TimerEntry(off_day, off_at, 0, relay)


def relay_run_upload(relay: int, seconds: int, now: datetime) -> ManualUpload:
    """Return the ``<n>Timermanual.json`` file for switching a relay on for ``seconds``."""
    return ManualUpload(
        relay_filename(relay), serialize_relay_manual(build_relay_run(relay, seconds, now))
    )


def relay_off_upload(relay: int, now: datetime) -> ManualUpload:
    """Return a ``<n>Timermanual.json`` whose only entry switches the relay off now.

    The payload the boiler Pi expects for "relay off" is still open (OQ-14); this is the file form
    named in FR-LEG-6 and is only used if the boiler Pi has no ``relayoff/`` call.
    """
    check_manual_relay_number(relay).raise_if_errors()
    day, at = day_and_time(_aware(now))
    return ManualUpload(
        relay_filename(relay), serialize_relay_manual([TimerEntry(day, at, 0, relay)])
    )


def _aware(now: datetime) -> datetime:
    """Return ``now`` if it is timezone-aware, otherwise raise."""
    if now.tzinfo is None:
        raise ManualError("a timezone-aware time is required")
    return now
