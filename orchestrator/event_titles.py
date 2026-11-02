"""The calendar event-title grammar (FR-CFG-6; ported from ``ReadGmailCalendar.parseevent``).

The *title* of a calendar event is the command; the event's own date and time give the day and the
start time. Case does not matter; ``; , : =`` and spaces all separate words.

Boiler::

    Boiler: target=3.5; min=2          (order free, extra words ignored)

Timer::

    Timer: relaynum 4 GardenRoof start sunset +30 end 23:00
    Timer: relaynum GardenLights duration 3600

* ``relaynum`` is followed by relay numbers and/or relay names (see :mod:`orchestrator.relays`);
* **a timer without any astro word** (neither at the start nor at the end) runs from the start to
  the end of the *calendar event itself*; ``end``, ``duration`` and a clock ``start`` in the title
  are then not needed and are ignored when the schedule is built (the title may still carry them);
* ``start`` is optional: without an astro word the timer starts at the event's start time;
* an astro word (``sunrise sunset dawn dusk noon midnight moonrise moonset twilight``) may be
  followed by ``+N`` or ``-N`` minutes. The first astro word is the start unless it comes after
  ``end``; a second one is the end. A single-digit offset is minutes, as in the effective legacy
  behaviour (the legacy hours conversion never worked);
* ``end`` is followed by a clock time (``23``, ``23 30`` or ``23:30:15``) or an astro word;
* ``duration`` is followed by seconds. If both an end and a duration are given, the end wins when
  the schedule is built (as in the legacy code). With an astro word in the title, an end or a
  duration is required.

The parser never raises on odd input: a title that is not a command gives no result, a title that
looks like a command but is wrong gives a ``problem`` text (the legacy code crashed on several of
these, for example a bare ``boiler`` or ``timer`` word, or ``target`` as the last word). Business
rules such as ``Target <= 5`` are not checked here; see :mod:`orchestrator.validation`.
"""

from dataclasses import dataclass
from datetime import time
from decimal import Decimal

from orchestrator.models import RELAY_MAX, RELAY_MIN, ModelError, format_number, to_decimal
from orchestrator.relays import RelayNames

ASTRO_NAMES = frozenset(
    {"sunrise", "sunset", "dawn", "dusk", "noon", "midnight", "moonrise", "moonset", "twilight"}
)
_KEYWORDS = ASTRO_NAMES | {"start", "end", "duration", "relaynum"}
_SEPARATORS = ";,:="
MAX_OFFSET_MINUTES = 24 * 60
"""Largest astro offset; more than a day makes no sense and could overflow later date arithmetic."""
MAX_DURATION_SECONDS = 24 * 60 * 60
"""Longest calendar timer; the same limit as a manual relay run."""


@dataclass(frozen=True, slots=True)
class AstroTime:
    """A moment given relative to the sun or moon, for example sunset + 30 minutes."""

    name: str
    offset_minutes: int = 0


@dataclass(frozen=True, slots=True)
class BoilerCommand:
    target: Decimal
    minimum: Decimal


@dataclass(frozen=True, slots=True)
class TimerCommand:
    relays: tuple[int, ...]
    start: AstroTime | None  # None: the event's own start time
    end_clock: time | None
    end_astro: AstroTime | None
    duration_s: int | None


@dataclass(frozen=True, slots=True)
class TitleResult:
    """What a title means: a command, nothing (an ordinary event), or a problem to report."""

    command: BoilerCommand | TimerCommand | None = None
    problem: str | None = None
    warnings: tuple[str, ...] = ()


def parse_title(title: str, relay_names: RelayNames | None = None) -> TitleResult:
    """Read an event title; see the module docstring for the grammar."""
    names = relay_names or RelayNames()
    lowered = title.lower()
    if "boiler:" in lowered:
        return _parse_boiler(_tokens(lowered, "boiler:"))
    if "timer:" in lowered:
        return _parse_timer(_tokens(lowered, "timer:"), names)
    return TitleResult()


def _is_whole(word: str) -> bool:
    """True for a non-empty run of plain ASCII digits (``str.isdigit`` also accepts ``²`` etc.)."""
    return word.isascii() and word.isdigit()


def _to_int(word: str) -> int:
    """Convert ASCII digits to an int; absurdly long numbers become 10**18, failing range checks."""
    return int(word) if len(word) <= 18 else 10**18


def _tokens(lowered: str, prefix: str) -> list[str]:
    text = lowered.replace("+", " + ").replace("-", " - ").replace(prefix, " ")
    for separator in _SEPARATORS:
        text = text.replace(separator, " ")
    return text.split()


# --- boiler -------------------------------------------------------------------------------------


def _number_after(tokens: list[str], keyword: str) -> tuple[Decimal | None, str | None]:
    if keyword not in tokens:
        return None, f"{keyword} is missing"
    index = tokens.index(keyword) + 1
    if index >= len(tokens):
        return None, f"{keyword} has no value"
    word = tokens[index]
    if not _is_whole(word.replace(".", "", 1)):
        return None, f"{keyword} must be a number (0 or more), got {word!r}"
    try:
        return to_decimal(word), None
    except ModelError:
        return None, f"{keyword} must be a number, got {word!r}"


def _parse_boiler(tokens: list[str]) -> TitleResult:
    target, target_problem = _number_after(tokens, "target")
    minimum, min_problem = _number_after(tokens, "min")
    problems = [p for p in (target_problem, min_problem) if p]
    if problems or target is None or minimum is None:
        return TitleResult(problem="Boiler event: " + "; ".join(problems))
    return TitleResult(BoilerCommand(target, minimum))


# --- timer --------------------------------------------------------------------------------------


def _parse_astro(tokens: list[str], index: int) -> tuple[AstroTime | None, int, str | None]:
    """Read an astro word at ``index`` and its optional ``+N`` / ``-N`` minutes."""
    name = tokens[index]
    following = tokens[index + 1 : index + 3]
    if following and following[0] in ("+", "-"):
        if len(following) < 2 or not _is_whole(following[1]):
            return None, index + 2, f"{name} {following[0]} must be followed by whole minutes"
        minutes = _to_int(following[1])
        if minutes > MAX_OFFSET_MINUTES:
            return None, index + 3, f"{name} offset must be at most {MAX_OFFSET_MINUTES} minutes"
        return AstroTime(name, minutes if following[0] == "+" else -minutes), index + 3, None
    return AstroTime(name), index + 1, None


def _parse_clock(numbers: list[str]) -> tuple[time | None, str | None]:
    if len(numbers) > 3:
        return None, "the end time has too many numbers (use hours, minutes, seconds)"
    hour, minute, second = (_to_int(n) for n in [*numbers, "0", "0"][:3])
    try:
        return time(hour, minute, second), None
    except (ValueError, OverflowError):  # out of range, or too large for a C integer
        return None, f"the end time {':'.join(numbers)} is not a valid time"


def _parse_timer(tokens: list[str], names: RelayNames) -> TitleResult:
    relays: list[int] = []
    warnings: list[str] = []
    start: AstroTime | None = None
    end_clock: time | None = None
    end_astro: AstroTime | None = None
    duration: int | None = None
    expecting_end = False
    index = 0
    while index < len(tokens):
        word = tokens[index]
        if word == "end":
            index += 1
            numbers: list[str] = []
            while index < len(tokens) and _is_whole(tokens[index]):
                numbers.append(tokens[index])
                index += 1
            if numbers:
                end_clock, problem = _parse_clock(numbers)
                if problem:
                    return TitleResult(problem="Timer event: " + problem)
                expecting_end = False
            else:
                expecting_end = True
        elif word == "start":
            expecting_end = False
            index += 1
        elif word == "duration":
            value = tokens[index + 1] if index + 1 < len(tokens) else ""
            if not _is_whole(value) or not 0 < _to_int(value) <= MAX_DURATION_SECONDS:
                return TitleResult(
                    problem=(
                        "Timer event: duration must be a number of seconds "
                        f"(1 to {MAX_DURATION_SECONDS})"
                    )
                )
            duration = _to_int(value)
            index += 2
        elif word == "relaynum":
            index += 1
            while index < len(tokens) and tokens[index] not in _KEYWORDS:
                item = tokens[index]
                index += 1
                if item in ("+", "-"):
                    continue
                relay = _to_int(item) if _is_whole(item) else names.lookup(item)
                if relay is None:
                    warnings.append(f"unknown relay name {item!r} ignored")
                elif not RELAY_MIN <= relay <= RELAY_MAX:
                    return TitleResult(problem=f"Timer event: relay {relay} does not exist")
                else:
                    if names.is_ambiguous(item):
                        warnings.append(
                            f"relay name {item!r} is listed for several relays; using {relay}"
                        )
                    if relay not in relays:
                        relays.append(relay)
        elif word in ASTRO_NAMES:
            astro, index, problem = _parse_astro(tokens, index)
            if problem or astro is None:
                return TitleResult(problem="Timer event: " + (problem or "bad astro time"))
            if not expecting_end or start is None:
                start, expecting_end = astro, True
            else:
                end_astro, expecting_end = astro, False
        else:
            index += 1
    if not relays:
        return TitleResult(
            problem="Timer event: no relay found after 'relaynum'", warnings=tuple(warnings)
        )
    has_astro = start is not None or end_astro is not None
    if has_astro and end_clock is None and end_astro is None and duration is None:
        return TitleResult(
            problem="Timer event: needs an end time or a duration", warnings=tuple(warnings)
        )
    return TitleResult(
        TimerCommand(tuple(relays), start, end_clock, end_astro, duration), warnings=tuple(warnings)
    )


# --- writing titles (the inverse of parse_title) --------------------------------------------------


class UnrepresentableError(ValueError):
    """A command cannot be written as an event title that reads back to the same command."""


def _astro_text(astro: AstroTime) -> str:
    if astro.offset_minutes == 0:
        return astro.name
    sign = "+" if astro.offset_minutes > 0 else "-"
    return f"{astro.name} {sign}{abs(astro.offset_minutes)}"


def format_boiler_title(command: BoilerCommand) -> str:
    """Write ``Boiler: target=<x>; min=<y>`` (numbers as they were entered)."""
    title = f"Boiler: target={format_number(command.target)}; min={format_number(command.minimum)}"
    return _checked(title, command, RelayNames())


def format_timer_title(command: TimerCommand, relay_names: RelayNames | None = None) -> str:
    """Write a ``Timer: relaynum ...`` title; raises :class:`UnrepresentableError` if impossible.

    The title is only returned if :func:`parse_title` reads it back to exactly the same command.
    One shape cannot be written: an astro *end* without a start (the grammar reads the first astro
    word as the start), so a start such as ``sunset`` must be given with it.
    """
    names = relay_names or RelayNames()
    if command.end_clock is not None and command.end_astro is not None:
        raise UnrepresentableError(
            "a timer cannot have both a clock end time and an astro end time"
        )
    if command.end_astro is not None and command.start is None:
        raise UnrepresentableError("an astro end time needs an astro start time as well")
    words = []
    for relay in command.relays:
        name = names.name_of(relay)
        words.append(name if name and names.lookup(name) == relay else str(relay))
    parts = ["Timer:", "relaynum", *words]
    if command.start is not None:
        parts += ["start", _astro_text(command.start)]
    if command.end_clock is not None:
        parts += ["end", command.end_clock.strftime("%H:%M:%S")]
    elif command.end_astro is not None:
        parts += ["end", _astro_text(command.end_astro)]
    if command.duration_s is not None:
        parts += ["duration", str(command.duration_s)]
    return _checked(" ".join(parts), command, names)


def _checked(title: str, command: BoilerCommand | TimerCommand, names: RelayNames) -> str:
    result = parse_title(title, names)
    if result.command != command or result.problem:
        reason = result.problem or "it would be read back as something else"
        raise UnrepresentableError(f"cannot be written as an event title: {reason}")
    return title
