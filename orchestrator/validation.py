"""Business-rule validation, shared by every input channel (FR-CFG-8, Requirements section 6).

Calendar events, UI edits, MQTT commands, e-mail commands and manual overrides all call the same
functions, so the heater limit of 5 showers cannot be bypassed by choosing a different channel.

Results are collected, not raised one at a time, so the UI and the e-mail reply can show every
problem at once:

* **errors** make the input unacceptable (nothing is saved or pushed);
* **warnings** are accepted but reported. ``Min`` above ``Target`` is a warning: the real
  ``Cal.json`` uses it on purpose (for example ``Target 3.0, Min 3.5`` at 13:00), meaning "keep at
  least 3.5"; the heater then follows Min.

Structural checks (day 1..7, whole-second times, finite numbers) live in :mod:`orchestrator.models`.
"""

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal

from orchestrator.config import HARD_MAX_SHOWERS as CONFIG_HARD_MAX_SHOWERS
from orchestrator.models import (
    RELAY_MAX,
    RELAY_MIN,
    BoilerSchedule,
    BoilerSlot,
    ManualEntry,
    TimerEntry,
    TimerSchedule,
    format_number,
    format_time,
)
from orchestrator.weekdays import day_name

HARD_MAX_SHOWERS = Decimal(str(CONFIG_HARD_MAX_SHOWERS))
"""The heater limit (one definition, in the config module). Nothing above it is ever accepted."""

MANUAL_RELAYS = frozenset(range(2, 8))
"""Relays 0 and 1 belong to the boiler service; manual commands may only use 2..7."""
MIN_RELAY_SECONDS = 10
MAX_RELAY_SECONDS = 24 * 60 * 60


class ValidationError(ValueError):
    """The input broke one or more rules; ``problems`` lists them all."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class ValidationResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_errors(self) -> None:
        if self.errors:
            raise ValidationError(self.errors)


# --- showers ----------------------------------------------------------------------------------


def check_levels(
    target: Decimal, minimum: Decimal, max_showers: Decimal = HARD_MAX_SHOWERS, label: str = ""
) -> ValidationResult:
    """Check a Target / Min pair against 0 <= value <= the heater limit.

    ``Min > Target`` is only a warning (see the module docstring).
    """
    limit = min(max_showers, HARD_MAX_SHOWERS)
    prefix = f"{label}: " if label else ""
    errors: list[str] = []
    for name, value in (("Target", target), ("Min", minimum)):
        if value < 0:
            errors.append(f"{prefix}{name} {format_number(value)} is below 0")
        elif value > limit:
            errors.append(f"{prefix}{name} {format_number(value)} is above the limit of {limit:f}")
    warnings: list[str] = []
    if not errors and minimum > target:
        warnings.append(
            f"{prefix}Min {format_number(minimum)} is above Target {format_number(target)}; "
            "the heater will keep Min"
        )
    return ValidationResult(tuple(errors), tuple(warnings))


def check_manual_entry(
    entry: ManualEntry, max_showers: Decimal = HARD_MAX_SHOWERS
) -> ValidationResult:
    """Check the Target / Min of a manual override (On: above the current showers; Off: 0 / 0)."""
    return check_levels(entry.target, entry.minimum, max_showers, label="manual override")


# --- boiler schedule --------------------------------------------------------------------------


def _slot_label(slot: BoilerSlot) -> str:
    return f"{day_name(slot.day)} {format_time(slot.start)}"


def check_boiler_schedule(
    schedule: BoilerSchedule, max_showers: Decimal = HARD_MAX_SHOWERS
) -> ValidationResult:
    """Check a whole boiler schedule: levels of every slot, no two slots at the same time."""
    if not schedule.slots:
        return ValidationResult(errors=("the boiler schedule has no slots",))
    errors: list[str] = []
    warnings: list[str] = []
    groups: dict[tuple[int, str], list[BoilerSlot]] = defaultdict(list)
    for slot in schedule.slots:
        result = check_levels(slot.target, slot.minimum, max_showers, label=_slot_label(slot))
        errors.extend(result.errors)
        warnings.extend(result.warnings)
        groups[(slot.day, format_time(slot.start))].append(slot)
    for slots in groups.values():
        if len(slots) > 1:
            errors.append(f"{_slot_label(slots[0])}: {len(slots)} different slots at the same time")
    return ValidationResult(tuple(errors), tuple(warnings))


# --- timer schedule ---------------------------------------------------------------------------


def _entry_label(entry: TimerEntry) -> str:
    return f"{day_name(entry.day)[:3]} {format_time(entry.at)}"


def check_timer_schedule(
    schedule: TimerSchedule,
    relays: Collection[int] = tuple(range(RELAY_MIN, RELAY_MAX + 1)),
) -> ValidationResult:
    """Check a timer schedule: relays known, no relay on and off at once, on/off alternate."""
    if not schedule.entries:
        return ValidationResult(errors=("the timer schedule has no entries",))
    errors: list[str] = []
    warnings: list[str] = []
    by_relay: dict[int, list[TimerEntry]] = defaultdict(list)
    at_same_time: dict[tuple[int, str, int], set[int]] = defaultdict(set)
    for entry in schedule.entries:
        if entry.relay not in relays:
            errors.append(f"{_entry_label(entry)}: relay {entry.relay} is not in the relay list")
        by_relay[entry.relay].append(entry)
        at_same_time[(entry.day, format_time(entry.at), entry.relay)].add(entry.state)
    for (day, at, relay), states in at_same_time.items():
        if len(states) > 1:
            errors.append(f"{day_name(day)[:3]} {at}: relay {relay} is switched on and off at once")

    for relay, entries in sorted(by_relay.items()):
        if len(entries) == 1:
            kind = "ON" if entries[0].state else "OFF"
            warnings.append(f"relay {relay}: only one entry ({kind} {_entry_label(entries[0])})")
            continue
        count = len(entries)
        for index in range(1 if count == 2 else count):  # a pair is compared once, not twice
            current, following = entries[index], entries[(index + 1) % count]
            if current.state == following.state:
                kind = "ON" if current.state else "OFF"
                warnings.append(
                    f"relay {relay}: two {kind} entries in a row "
                    f"({_entry_label(current)} and {_entry_label(following)})"
                )
    return ValidationResult(tuple(errors), tuple(warnings))


# --- manual relay commands --------------------------------------------------------------------


def check_manual_relay_number(relay: int) -> ValidationResult:
    """Check that a relay may be switched manually (relays 2..7 only)."""
    if isinstance(relay, bool) or relay not in MANUAL_RELAYS:
        return ValidationResult(
            errors=(
                f"relay {relay} cannot be switched manually; allowed: "
                f"{min(MANUAL_RELAYS)}-{max(MANUAL_RELAYS)} (relays 0 and 1 belong to the boiler)",
            )
        )
    return ValidationResult()


def check_manual_relay(relay: int, seconds: int) -> ValidationResult:
    """Check a manual relay request (relays 2..7, more than 10 s and at most 24 h)."""
    errors: list[str] = list(check_manual_relay_number(relay).errors)
    if isinstance(seconds, bool) or not MIN_RELAY_SECONDS < seconds <= MAX_RELAY_SECONDS:
        errors.append(
            f"duration {seconds} s is not allowed; it must be more than {MIN_RELAY_SECONDS} s "
            f"and at most {MAX_RELAY_SECONDS} s"
        )
    return ValidationResult(tuple(errors))
