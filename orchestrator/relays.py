"""Relay names (Requirements section 3.9).

Calendar titles may name a relay instead of giving its number
(``Timer: relaynum GardenLights ...``). The list comes from the configuration; the default is the
list of the legacy ``Name2RelayNum.py``.

Names are matched case-insensitively and must be a single word without the separators the title
parser uses (``; , : = + -`` and spaces). A name that is listed for two relays (the legacy list
has ``spareGround2`` for relays 6 and 7) is ambiguous: the first listing wins, as in the legacy
code, and the name is reported in :attr:`RelayNames.duplicates` so the start-up can warn about it.
"""

import re
from collections.abc import Iterable

from orchestrator.models import RELAY_MAX, RELAY_MIN, ModelError

DEFAULT_RELAY_NAME_PAIRS: tuple[tuple[str, int], ...] = (
    ("BoilerHeat", 0),
    ("BolerLight", 1),  # spelled as in the legacy list; calendar titles must use this spelling
    ("GardenRoof", 2),
    ("SpareRoof", 3),
    ("GardenLights", 4),
    ("intercom", 5),
    ("spareGround2", 6),
    ("spareGround2", 7),
)

_NAME = re.compile(r"[A-Za-z0-9_]+")


class RelayNames:
    """Lookup between relay names and relay numbers."""

    def __init__(self, pairs: Iterable[tuple[str, int]] = DEFAULT_RELAY_NAME_PAIRS) -> None:
        self._by_name: dict[str, int] = {}
        self._by_relay: dict[int, str] = {}
        duplicates: list[str] = []
        for name, relay in pairs:
            if not _NAME.fullmatch(name):
                raise ModelError("a relay name must be one word of letters, digits or underscores")
            if isinstance(relay, bool) or not RELAY_MIN <= relay <= RELAY_MAX:
                raise ModelError(f"relay number must be {RELAY_MIN}..{RELAY_MAX}")
            key = name.lower()
            if key in self._by_name:
                if name not in duplicates:
                    duplicates.append(name)
            else:
                self._by_name[key] = relay
            self._by_relay.setdefault(relay, name)
        self.duplicates: tuple[str, ...] = tuple(duplicates)

    def lookup(self, word: str) -> int | None:
        """Return the relay number for a name (any case), or ``None`` if the name is unknown."""
        return self._by_name.get(word.strip().lower())

    def is_ambiguous(self, word: str) -> bool:
        """True if the name is listed for more than one relay (the first listing is used)."""
        return word.strip().lower() in {name.lower() for name in self.duplicates}

    def name_of(self, relay: int) -> str | None:
        """Return the name listed for a relay, for the UI and the e-mails."""
        return self._by_relay.get(relay)
