"""Reading and checking the JSON a browser sends to the API (CS-15).

Every function raises :class:`InputError` with a message that is safe to show to the user; nothing
is clamped or guessed. The limit of 5 showers is applied later by the editor and the manual
commands, so it is enforced in one place for every channel.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from orchestrator.event_titles import ASTRO_NAMES, AstroTime, BoilerCommand, TimerCommand
from orchestrator.models import ModelError, parse_time, to_decimal

MAX_RELAY_NUMBER = 99
MAX_OFFSET_MINUTES = 1440
MAX_DURATION_S = 86400


class InputError(ValueError):
    """The request is not acceptable; the text is meant for the user."""


@dataclass(frozen=True, slots=True)
class TimerInput:
    command: TimerCommand
    event_time: time  # the clock time of the calendar event (the start unless it is an astro time)


def require_object(data: object) -> Mapping[str, Any]:
    if not isinstance(data, dict):
        raise InputError("expected a JSON object")
    return data


def _only(body: Mapping[str, Any], allowed: Sequence[str]) -> None:
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise InputError("unknown field(s): " + ", ".join(str(key)[:30] for key in unknown))


def whole_number(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InputError(f"{name}: expected a whole number between {low} and {high}")
    return value


def parse_clock(value: object, name: str) -> time:
    if not isinstance(value, str) or len(value) > 8:
        raise InputError(f"{name}: expected a time such as 06:30")
    text = value + ":00" if len(value) == 5 else value
    try:
        return parse_time(text)
    except ModelError:
        raise InputError(f"{name}: expected a time such as 06:30") from None


def parse_days(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 7:
        raise InputError(
            "days: expected a list of 1 to 7 day numbers (1 = Sunday ... 7 = Saturday)"
        )
    days = [whole_number(day, "days", 1, 7) for day in value]
    if len(set(days)) != len(days):
        raise InputError("days: a day is listed twice")
    return tuple(sorted(days))


def parse_boiler(body: Mapping[str, Any]) -> BoilerCommand:
    """Target and Min in showers; they may be numbers or numeric text."""
    values: dict[str, Any] = {}
    for key, name in (("target", "target"), ("min", "min")):
        raw = body.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int | float | str):
            raise InputError(f"{name}: expected a number of showers")
        try:
            values[key] = to_decimal(raw)
        except ModelError:
            raise InputError(f"{name}: expected a number of showers") from None
    return BoilerCommand(target=values["target"], minimum=values["min"])


def _astro(value: object, name: str) -> AstroTime:
    obj = require_object(value)
    _only(obj, ("astro", "offset"))
    word = obj.get("astro")
    if not isinstance(word, str) or word.lower() not in ASTRO_NAMES:
        raise InputError(f"{name}: astro must be one of " + ", ".join(sorted(ASTRO_NAMES)))
    offset = whole_number(
        obj.get("offset", 0), f"{name} offset", -MAX_OFFSET_MINUTES, MAX_OFFSET_MINUTES
    )
    return AstroTime(word.lower(), offset)


def parse_timer(body: Mapping[str, Any]) -> TimerInput:
    """``relays``, a start (clock ``time`` or ``start_astro``) and an end (one of three forms)."""
    relays_raw = body.get("relays")
    if not isinstance(relays_raw, list) or not 1 <= len(relays_raw) <= 8:
        raise InputError("relays: expected a list of 1 to 8 relay numbers")
    relays = tuple(whole_number(r, "relays", 0, MAX_RELAY_NUMBER) for r in relays_raw)
    if len(set(relays)) != len(relays):
        raise InputError("relays: a relay is listed twice")
    start_astro = _astro(body["start_astro"], "start_astro") if "start_astro" in body else None
    event_time = parse_clock(body.get("time", "00:00" if start_astro else None), "time")
    forms = [key for key in ("end", "end_astro", "duration_s") if key in body]
    if len(forms) != 1:
        raise InputError("give exactly one of end, end_astro or duration_s")
    end_clock = end_astro = duration = None
    if forms[0] == "end":
        end_clock = parse_clock(body["end"], "end")
    elif forms[0] == "end_astro":
        end_astro = _astro(body["end_astro"], "end_astro")
    else:
        duration = whole_number(body["duration_s"], "duration_s", 1, MAX_DURATION_S)
    command = TimerCommand(relays, start_astro, end_clock, end_astro, duration)
    return TimerInput(command, event_time)


def parse_edited_at(value: object) -> datetime | None:
    """The moment the page loaded the item, as an ISO text with a time zone."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise InputError("edited_at: expected an ISO date and time")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise InputError("edited_at: expected an ISO date and time") from None
    if moment.tzinfo is None:
        raise InputError("edited_at: the time zone is missing")
    return moment


def check_fields(body: Mapping[str, Any], allowed: Sequence[str]) -> None:
    _only(body, allowed)


BOILER_FIELDS = ("days", "time", "target", "min", "edited_at")
TIMER_FIELDS = (
    "days",
    "time",
    "relays",
    "start_astro",
    "end",
    "end_astro",
    "duration_s",
    "edited_at",
)
