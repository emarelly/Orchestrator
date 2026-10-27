"""Facts about the Pi itself, for the health endpoint and the diagnostics (OPS-8, OPS-9, OPS-11).

Every value is ``None`` when it cannot be read (other operating system, missing file), never a
guess. Reading is cheap, but the clock-sync check starts a program, so :class:`SystemFacts` keeps
the answer for a while.
"""

import logging
import mmap
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator.logging_setup import kv

logger = logging.getLogger(__name__)

PROC_STATM = Path("/proc/self/statm")
THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
CACHE_SECONDS = 30.0


def memory_rss_mb(statm: Path = PROC_STATM) -> float | None:
    """Resident memory of this process in MB (Linux)."""
    try:
        pages = int(statm.read_text(encoding="ascii").split()[1])
        return float(round(pages * mmap.PAGESIZE / (1024 * 1024), 1))
    except (OSError, ValueError, IndexError):
        return None


def cpu_temperature_c(path: Path = THERMAL) -> float | None:
    """CPU temperature in degrees Celsius (the Pi reports millidegrees)."""
    try:
        return round(int(path.read_text(encoding="ascii").strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def disk_free_percent(path: Path) -> float | None:
    """Free space of the disk that holds ``path``, in percent."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return round(usage.free * 100 / usage.total, 1) if usage.total else None


def clock_synchronized() -> bool | None:
    """Whether the system clock is synchronized (``timedatectl``, a fixed command)."""
    try:
        done = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    answer = done.stdout.strip().lower()
    return {"yes": True, "no": False}.get(answer)


class SystemFacts:
    """The facts, read at most every ``CACHE_SECONDS`` seconds."""

    def __init__(
        self,
        data_dir: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        memory: Callable[[], float | None] = memory_rss_mb,
        temperature: Callable[[], float | None] = cpu_temperature_c,
        clock_sync: Callable[[], bool | None] = clock_synchronized,
    ) -> None:
        self._data_dir = data_dir
        self._monotonic = monotonic
        self._readers: dict[str, Callable[[], Any]] = {
            "memory_rss_mb": memory,
            "cpu_temp_c": temperature,
            "disk_free_percent": lambda: disk_free_percent(self._data_dir),
            "clock_synchronized": clock_sync,
        }
        self._lock = threading.Lock()
        self._read_at: float | None = None
        self._values: dict[str, Any] = {}

    def read(self) -> dict[str, Any]:
        with self._lock:
            now = self._monotonic()
            if self._read_at is None or now - self._read_at >= CACHE_SECONDS:
                self._values = {
                    name: self._safely(name, read) for name, read in self._readers.items()
                }
                self._read_at = now
            return dict(self._values)

    @staticmethod
    def _safely(name: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception:  # a facts reader must never take the health check down
            logger.warning(kv("system_fact_failed", fact=name))
            return None
