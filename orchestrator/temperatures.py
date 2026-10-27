"""Assigning the three boiler temperatures to Upper / Mid / Lower (FR-UI-1b).

Each reading belongs to a fixed sensor position, as in the legacy page: ``CurrentTemp[2]`` is Upper,
``CurrentTemp[0]`` is Mid and ``CurrentTemp[1]`` is Lower. The readings are **not** sorted by value:
on a hot day the incoming water at the bottom can for a while be warmer than the water above it,
and sorting would then move the readings to the wrong place. The UI and the MQTT entities both use
this module, so they always agree.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from orchestrator.models import BoilerStatus, ModelError

# index of each position in the boiler Pi's ``CurrentTemp`` list
UPPER_INDEX = 2
MID_INDEX = 0
LOWER_INDEX = 1


@dataclass(frozen=True, slots=True)
class TemperatureLevels:
    """Temperatures in degrees Celsius, by position in the boiler."""

    upper: float
    mid: float
    lower: float

    def as_dict(self) -> dict[str, float]:
        """Return ``{"upper": ..., "mid": ..., "lower": ...}`` (for the API and MQTT)."""
        return {"upper": self.upper, "mid": self.mid, "lower": self.lower}


def assign_levels(temps: Sequence[float]) -> TemperatureLevels:
    """Put the three readings, in the order of the boiler Pi's list, into their positions."""
    if len(temps) != 3:
        raise ModelError(f"exactly three temperatures are needed, got {len(temps)}")
    if not all(math.isfinite(value) for value in temps):
        raise ModelError("temperatures must be finite numbers")
    return TemperatureLevels(
        upper=temps[UPPER_INDEX], mid=temps[MID_INDEX], lower=temps[LOWER_INDEX]
    )


def levels_of(status: BoilerStatus) -> TemperatureLevels:
    """Return the temperature levels of a boiler status."""
    return assign_levels(status.temps)
