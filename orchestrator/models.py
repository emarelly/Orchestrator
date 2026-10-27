"""Domain models (CS-3): schedules, manual files and the boiler status.

Everything here is a frozen dataclass and pure data: no I/O, no clock. Days of the week use the
boiler Pi's numbering, 1 = Sunday ... 7 = Saturday. Times are whole seconds (``HH:MM:SS``).

Target and Min are :class:`~decimal.Decimal` so that the number *as written* survives a round trip
(``3`` stays ``3`` and ``3.0`` stays ``3.0``); the boiler and timer services read these files with a
string-splitting parser and the existing ``Cal.json`` mixes both spellings (FR-PUSH-7).

The models check structure only (ranges of day, state and relay, whole-second times, finite
numbers). Business rules such as ``Target <= 5`` live in the validation module so that they apply,
in one place, to every input channel.
"""

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Self

DAY_MIN = 1
DAY_MAX = 7
RELAY_MIN = 0
RELAY_MAX = 7


class ModelError(ValueError):
    """A value does not fit the model."""


class StatusParseError(ModelError):
    """The boiler Pi's ``status/`` answer is missing something or has the wrong type."""


# --- small value helpers ----------------------------------------------------------------------

_TIME = re.compile(r"(\d{2}):(\d{2})(?::(\d{2}))?")


def parse_time(text: str) -> time:
    """Parse ``HH:MM:SS`` (or ``HH:MM``) into a whole-second time."""
    match = _TIME.fullmatch(text.strip())
    if match is None:
        raise ModelError(f"time must look like HH:MM:SS, got {text!r}")
    hour, minute, second = (int(part or 0) for part in match.groups())
    try:
        return time(hour, minute, second)
    except ValueError:
        raise ModelError(f"time out of range: {text!r}") from None


def format_time(value: time) -> str:
    """Format as ``HH:MM:SS``, the spelling the boiler and timer services expect."""
    return f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"


def to_decimal(value: str | int | float | Decimal) -> Decimal:
    """Convert a number from any input channel to a finite Decimal (floats via their text)."""
    if isinstance(value, bool):
        raise ModelError("a number is required, not a boolean")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation:
        raise ModelError(f"not a number: {value!r}") from None
    if not result.is_finite():
        raise ModelError(f"not a finite number: {value!r}")
    return result


def format_number(value: Decimal) -> str:
    """Format a Decimal the way it was written, never in exponent form."""
    return format(value, "f")


def _check_day(day: int) -> None:
    if isinstance(day, bool) or not isinstance(day, int) or not DAY_MIN <= day <= DAY_MAX:
        raise ModelError(f"day of week must be {DAY_MIN}..{DAY_MAX} (1 = Sunday), got {day!r}")


def _check_time(value: time) -> None:
    if not isinstance(value, time) or value.microsecond or value.tzinfo is not None:
        raise ModelError("time must be a whole-second time without a time zone")


def _check_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ModelError(f"{name} must be a finite Decimal")


# --- boiler schedule --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoilerSlot:
    """One entry of ``Cal.json``.

    From ``start`` until the next slot, keep at least ``minimum`` and heat up to ``target``.
    """

    day: int
    start: time
    target: Decimal
    minimum: Decimal

    def __post_init__(self) -> None:
        _check_day(self.day)
        _check_time(self.start)
        _check_decimal("target", self.target)
        _check_decimal("minimum", self.minimum)

    @classmethod
    def create(
        cls,
        day: int,
        start: time | str,
        target: str | int | float | Decimal,
        minimum: str | int | float | Decimal,
    ) -> Self:
        """Build a slot from loosely typed input (calendar text, UI numbers, JSON values)."""
        when = parse_time(start) if isinstance(start, str) else start
        return cls(day, when, to_decimal(target), to_decimal(minimum))

    @property
    def sort_key(self) -> tuple[int, time]:
        return (self.day, self.start)


@dataclass(frozen=True, slots=True)
class BoilerSchedule:
    """The whole boiler schedule: sorted by day and time, without exact duplicates."""

    slots: tuple[BoilerSlot, ...] = ()

    def __post_init__(self) -> None:
        if self.slots != self._normalized(self.slots):
            raise ModelError(
                "slots must be sorted by day and time without exact duplicates; use of()"
            )

    @staticmethod
    def _normalized(slots: Iterable[BoilerSlot]) -> tuple[BoilerSlot, ...]:
        seen: set[BoilerSlot] = set()
        result: list[BoilerSlot] = []
        for slot in sorted(slots, key=lambda item: item.sort_key):  # stable: equal keys keep order
            if slot not in seen:
                seen.add(slot)
                result.append(slot)
        return tuple(result)

    @classmethod
    def of(cls, slots: Iterable[BoilerSlot]) -> Self:
        """Sort and de-duplicate (the legacy ``RemoveDuplicate``, applied to all duplicates)."""
        return cls(cls._normalized(slots))

    def __len__(self) -> int:
        return len(self.slots)


# --- timer schedule ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimerEntry:
    """One entry of ``TimeCal.json`` (or of a ``<n>Timermanual.json``): switch a relay at a time."""

    day: int
    at: time
    state: int  # 1 = on, 0 = off
    relay: int

    def __post_init__(self) -> None:
        _check_day(self.day)
        _check_time(self.at)
        if self.state not in (0, 1) or isinstance(self.state, bool):
            raise ModelError(f"state must be 0 or 1, got {self.state!r}")
        if isinstance(self.relay, bool) or not RELAY_MIN <= self.relay <= RELAY_MAX:
            raise ModelError(f"relay must be {RELAY_MIN}..{RELAY_MAX}, got {self.relay!r}")

    @property
    def sort_key(self) -> tuple[int, time]:
        return (self.day, self.at)


@dataclass(frozen=True, slots=True)
class TimerSchedule:
    """The whole timer schedule: sorted by day and time, without exact duplicates."""

    entries: tuple[TimerEntry, ...] = ()

    def __post_init__(self) -> None:
        if self.entries != self._normalized(self.entries):
            raise ModelError("entries must be sorted by day and time without duplicates; use of()")

    @staticmethod
    def _normalized(entries: Iterable[TimerEntry]) -> tuple[TimerEntry, ...]:
        seen: set[TimerEntry] = set()
        result: list[TimerEntry] = []
        for entry in sorted(entries, key=lambda item: item.sort_key):
            if entry not in seen:
                seen.add(entry)
                result.append(entry)
        return tuple(result)

    @classmethod
    def of(cls, entries: Iterable[TimerEntry]) -> Self:
        """Sort and de-duplicate."""
        return cls(cls._normalized(entries))

    def __len__(self) -> int:
        return len(self.entries)


# --- manual files -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManualEntry:
    """The single slot of ``manual.json``. Unlike a schedule slot, day and time are the *expiry*."""

    expires_day: int
    expires_at: time
    target: Decimal
    minimum: Decimal

    def __post_init__(self) -> None:
        _check_day(self.expires_day)
        _check_time(self.expires_at)
        _check_decimal("target", self.target)
        _check_decimal("minimum", self.minimum)


@dataclass(frozen=True, slots=True)
class ManualOverride:
    """Content of ``manual.json``: one entry (manual On/Off) or ``None`` for the empty file."""

    entry: ManualEntry | None = None

    @property
    def is_auto(self) -> bool:
        return self.entry is None


# --- boiler status ----------------------------------------------------------------------------

_HEATER = {"on": True, "off": False}
_ACTIVE = {"yes": True, "no": False}


@dataclass(frozen=True, slots=True)
class BoilerStatus:
    """The answer of ``GET status/`` (Requirements section 3.5)."""

    mode: str  # "Auto", "Manual" or "Off"; any other text is kept and shown as it is
    heater_on: bool
    active: bool
    showers_linear: float  # CurrentShowers (old calculation), already in showers
    showers_thermo: float  # CurrentShowersnew (thermodynamic calculation)
    temps: tuple[float, float, float]  # CurrentTemp, in the order the Pi sends them
    target: float
    minimum: float
    minutes_to_start: int
    date: datetime  # local time of the Pi, no time zone in the text

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> Self:
        """Build a status from decoded JSON; raises :class:`StatusParseError` if it is unusable."""
        temps = _field(payload, "CurrentTemp")
        if (
            not isinstance(temps, Sequence)
            or isinstance(temps, str)
            or len(temps) != 3
            or not all(_is_number(item) for item in temps)
        ):
            raise StatusParseError("CurrentTemp: expected a list of three numbers")
        mode = _field(payload, "Status")
        if not isinstance(mode, str) or not mode:
            raise StatusParseError("Status: expected text")
        minutes = _field(payload, "TimeToStartMin")
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise StatusParseError("TimeToStartMin: expected a whole number")
        return cls(
            mode=mode,
            heater_on=_choice(payload, "HeaterOnOff", _HEATER),
            active=_choice(payload, "ActiveYesNo", _ACTIVE),
            showers_linear=_number(payload, "CurrentShowers"),
            showers_thermo=_number(payload, "CurrentShowersnew"),
            temps=(float(temps[0]), float(temps[1]), float(temps[2])),
            target=_number(payload, "TargetVal"),
            minimum=_number(payload, "MinVal"),
            minutes_to_start=minutes,
            date=_date(payload, "Date"),
        )

    def age(self, local_now: datetime) -> timedelta:
        """How old the reading is; ``local_now`` is the current local time (aware or naive)."""
        return local_now.replace(tzinfo=None) - self.date


class HistoryParseError(ModelError):
    """The ``history/`` answer is not the expected list of samples."""


@dataclass(frozen=True, slots=True)
class HistorySample:
    """One sample of the 24 h history (``history/``), as used by the chart."""

    at: datetime  # local time of the Pi, no time zone
    showers_linear: float
    minimum: float
    target: float
    heater: float
    showers_thermo: float


_LEADING_ZEROS = re.compile(r"(?<![\d.])0+(?=\d)")


def parse_history_text(text: str) -> tuple[HistorySample, ...]:
    """Read the raw ``history/`` answer of the boiler Pi.

    The boiler service sends a bare list of rows (``[..],[..]`` without the outer brackets) and
    zero-pads the time fields (``06``, ``09``), which is not valid JSON; the legacy page evaluates
    it as JavaScript. Both quirks are repaired here, a proper JSON list is read as it is.
    """
    body = _LEADING_ZEROS.sub("", text.strip()).rstrip(",")
    if not body.startswith("[["):
        body = f"[{body}]"
    try:
        payload = json.loads(body)
    except ValueError:
        raise HistoryParseError("history: the answer is not a list of rows") from None
    return parse_history(payload)


def parse_history(payload: object) -> tuple[HistorySample, ...]:
    """Read the decoded ``history/`` answer.

    Each row is ``[year, month, day, hour, minute, second, showersOld, min, target, heater,
    showersNew]``. The month is counted from 1 (January = 1), as the boiler Pi writes it. The legacy
    page feeds it straight into a JavaScript ``Date`` (0-based), so its chart dates are a month
    ahead; only the time of day was ever readable there.
    """
    if not isinstance(payload, Sequence) or isinstance(payload, str):
        raise HistoryParseError("history: expected a list of rows")
    samples: list[HistorySample] = []
    for index, row in enumerate(payload):
        if not isinstance(row, Sequence) or isinstance(row, str) or len(row) != 11:
            raise HistoryParseError(f"history row {index}: expected 11 numbers")
        if not all(_is_number(item) for item in row):
            raise HistoryParseError(f"history row {index}: all values must be numbers")
        year, month, day, hour, minute, second = (int(item) for item in row[:6])
        try:
            when = datetime(year, month, day, hour, minute, second)
        except ValueError:
            raise HistoryParseError(f"history row {index}: not a valid date and time") from None
        values = [float(item) for item in row[6:]]
        samples.append(HistorySample(when, values[0], values[1], values[2], values[3], values[4]))
    return tuple(samples)


def _field(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise StatusParseError(f"{key}: missing")
    return payload[key]


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _number(payload: Mapping[str, Any], key: str) -> float:
    value = _field(payload, key)
    if not _is_number(value):
        raise StatusParseError(f"{key}: expected a number")
    return float(value)


def _choice(payload: Mapping[str, Any], key: str, allowed: Mapping[str, bool]) -> bool:
    value = _field(payload, key)
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise StatusParseError(f"{key}: expected one of {', '.join(sorted(allowed))}")
    return allowed[value.strip().lower()]


def _date(payload: Mapping[str, Any], key: str) -> datetime:
    value = _field(payload, key)
    if not isinstance(value, str):
        raise StatusParseError(f"{key}: expected an ISO date and time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise StatusParseError(f"{key}: expected an ISO date and time") from None
    return parsed.replace(tzinfo=None)
