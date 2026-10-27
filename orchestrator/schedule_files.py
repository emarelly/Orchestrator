"""Reading and writing the schedule files (FR-PUSH-7, CS-14).

The boiler and timer services on the boiler Pi read ``Cal.json``, ``TimeCal.json``, ``manual.json``
and ``<n>Timermanual.json`` with a *string-splitting* parser, not a JSON parser (Requirements 3.7).
It only copes with the exact text the legacy writer produced, so the serializers here write exactly
that and nothing else:

* compact: no spaces and no newline, objects separated by ``,`` so the text contains ``},{``;
* no byte-order mark, UTF-8 (in practice plain ASCII), no trailing newline;
* key order ``DayOfWeek, Hour, Target, Min`` (boiler) or ``DayOfWeek, Hour, State, RelayNum``
  (timer), ``Hour`` always ``HH:MM:SS``, numbers unquoted and spelled as they were entered;
* entries sorted by day and time, without duplicates;
* an empty *schedule* is never written (the legacy writer would produce an invalid ``]``); the only
  empty file is the empty ``manual.json`` (``[]``), which means "no override" (Auto).

The parsers are tolerant (they use a real JSON parser, accept white space and a byte-order mark and
normalize order), but strict about content: exact keys, right types, valid values.
"""

import json
import re
from collections.abc import Iterable, Mapping
from datetime import time
from decimal import Decimal
from typing import Any, NoReturn

from orchestrator.models import (
    BoilerSchedule,
    BoilerSlot,
    ManualEntry,
    ManualOverride,
    ModelError,
    TimerEntry,
    TimerSchedule,
    format_number,
    format_time,
    parse_time,
)

_BOILER_KEYS = frozenset({"DayOfWeek", "Hour", "Target", "Min"})
_TIMER_KEYS = frozenset({"DayOfWeek", "Hour", "State", "RelayNum"})
_HOUR = re.compile(r"\d\d:\d\d:\d\d")


class ScheduleFileError(ValueError):
    """A schedule file cannot be written or does not have the expected content."""


# --- writing ----------------------------------------------------------------------------------


def _boiler_object(day: int, at: time, target: Decimal, minimum: Decimal) -> str:
    return (
        f'{{"DayOfWeek":{day},"Hour":"{format_time(at)}",'
        f'"Target":{format_number(target)},"Min":{format_number(minimum)}}}'
    )


def _timer_object(entry: TimerEntry) -> str:
    return (
        f'{{"DayOfWeek":{entry.day},"Hour":"{format_time(entry.at)}",'
        f'"State":{entry.state},"RelayNum":{entry.relay}}}'
    )


def _encode(objects: Iterable[str]) -> bytes:
    return ("[" + ",".join(objects) + "]").encode("utf-8")


def serialize_boiler_schedule(schedule: BoilerSchedule) -> bytes:
    """Return the bytes of ``Cal.json`` for a schedule (never empty)."""
    if not schedule.slots:
        raise ScheduleFileError("an empty boiler schedule is never written")
    return _encode(_boiler_object(s.day, s.start, s.target, s.minimum) for s in schedule.slots)


def serialize_timer_schedule(schedule: TimerSchedule) -> bytes:
    """Return the bytes of ``TimeCal.json`` for a schedule (never empty)."""
    if not schedule.entries:
        raise ScheduleFileError("an empty timer schedule is never written")
    return _encode(_timer_object(entry) for entry in schedule.entries)


def serialize_relay_manual(entries: Iterable[TimerEntry]) -> bytes:
    """Return the bytes of a ``<n>Timermanual.json``: the on and off entries of one relay run."""
    schedule = TimerSchedule.of(entries)
    if not schedule.entries:
        raise ScheduleFileError("a relay manual file needs at least one entry")
    return _encode(_timer_object(entry) for entry in schedule.entries)


def serialize_manual(override: ManualOverride) -> bytes:
    """Return the bytes of ``manual.json``: one entry, or ``[]`` for Auto."""
    entry = override.entry
    if entry is None:
        return b"[]"
    return _encode(
        [_boiler_object(entry.expires_day, entry.expires_at, entry.target, entry.minimum)]
    )


# --- reading ----------------------------------------------------------------------------------


def _reject_constant(name: str) -> NoReturn:
    raise ScheduleFileError(f"{name} is not allowed in a schedule file")


def _load(data: bytes | str) -> list[Mapping[str, Any]]:
    try:
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data.lstrip("\ufeff")
        raw = json.loads(
            text, parse_float=Decimal, parse_int=Decimal, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ScheduleFileError("the file is not valid JSON text") from None
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ScheduleFileError("the file must hold a list of objects")
    return raw


def _whole(item: Mapping[str, Any], key: str, index: int) -> int:
    value = item[key]
    if isinstance(value, Decimal) and value.is_finite() and value.as_tuple().exponent == 0:
        return int(value)
    raise ScheduleFileError(f"entry {index}: {key} must be a whole number")


def _number(item: Mapping[str, Any], key: str, index: int) -> Decimal:
    value = item[key]
    if isinstance(value, Decimal) and value.is_finite():
        return value
    raise ScheduleFileError(f"entry {index}: {key} must be a number")


def _hour(item: Mapping[str, Any], index: int) -> time:
    value = item["Hour"]
    if not isinstance(value, str) or not _HOUR.fullmatch(value):
        raise ScheduleFileError(f"entry {index}: Hour must look like HH:MM:SS")
    try:
        return parse_time(value)
    except ModelError:
        raise ScheduleFileError(f"entry {index}: Hour is not a valid time") from None


def _check_keys(item: Mapping[str, Any], keys: frozenset[str], index: int) -> None:
    if set(item) != keys:
        raise ScheduleFileError(f"entry {index}: expected exactly the keys {sorted(keys)}")


def _boiler_fields(item: Mapping[str, Any], index: int) -> tuple[int, time, Decimal, Decimal]:
    _check_keys(item, _BOILER_KEYS, index)
    return (
        _whole(item, "DayOfWeek", index),
        _hour(item, index),
        _number(item, "Target", index),
        _number(item, "Min", index),
    )


def parse_boiler_schedule(data: bytes | str) -> BoilerSchedule:
    """Read ``Cal.json`` content; sorts and removes exact duplicates."""
    items = _load(data)
    if not items:
        raise ScheduleFileError("the boiler schedule is empty")
    try:
        return BoilerSchedule.of(
            BoilerSlot(day, at, target, minimum)
            for day, at, target, minimum in (
                _boiler_fields(item, i) for i, item in enumerate(items)
            )
        )
    except ModelError as error:
        raise ScheduleFileError(str(error)) from None


def parse_timer_schedule(data: bytes | str) -> TimerSchedule:
    """Read ``TimeCal.json`` (or a relay manual file); sorts and removes exact duplicates."""
    items = _load(data)
    if not items:
        raise ScheduleFileError("the timer schedule is empty")
    entries: list[TimerEntry] = []
    try:
        for index, item in enumerate(items):
            _check_keys(item, _TIMER_KEYS, index)
            entries.append(
                TimerEntry(
                    _whole(item, "DayOfWeek", index),
                    _hour(item, index),
                    _whole(item, "State", index),
                    _whole(item, "RelayNum", index),
                )
            )
    except ModelError as error:
        raise ScheduleFileError(str(error)) from None
    return TimerSchedule.of(entries)


def parse_manual(data: bytes | str) -> ManualOverride:
    """Read ``manual.json`` content: ``[]`` is Auto, one entry is an override."""
    items = _load(data)
    if not items:
        return ManualOverride()
    if len(items) > 1:
        raise ScheduleFileError("manual.json holds at most one entry")
    day, at, target, minimum = _boiler_fields(items[0], 0)
    try:
        return ManualOverride(ManualEntry(day, at, target, minimum))
    except ModelError as error:
        raise ScheduleFileError(str(error)) from None
