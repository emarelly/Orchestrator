"""Performance check of a running Orchestrator (NFR-1a): memory, CPU and response times.

Run it ON THE Pi 3 while the service runs (and the UI may be open in a browser), for one hour:

    python -m tools.perf_check --duration 3600

It finds the service process through ``systemctl`` (or give ``--pid``), samples the resident memory
and the CPU use of that process, and plays two browser clients that ask the web API the way the
page does (status every 10 s, schedules every 30 s, system and history now and then). At the end it
compares with the budgets of NFR-1a and prints PASS or FAIL for each. The budgets are acceptance
criteria of milestone M8.

Memory and CPU are read from ``/proc``, so on other systems only the response times are measured.
The check only reads from the service's own web API; it sends no command.
"""

import argparse
import http.client
import json
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROC = Path("/proc")
CLOCK_TICKS = 100  # /proc/<pid>/stat counts in 1/100 s on Linux for Python's purposes


@dataclass(frozen=True, slots=True)
class Budgets:
    memory_steady_mb: float = 120.0
    memory_peak_mb: float = 150.0
    cpu_average_pct: float = 5.0
    cpu_peak_pct: float = 50.0
    cpu_peak_seconds: float = 5.0
    api_p95_ms: float = 100.0
    first_page_s: float = 2.0


@dataclass(frozen=True, slots=True)
class Sample:
    at: float  # seconds since the start of the run
    rss_mb: float | None
    cpu_pct: float | None  # of one core, over the interval before this sample


@dataclass(frozen=True, slots=True)
class Finding:
    name: str
    value: float | None
    limit: float
    unit: str

    @property
    def ok(self) -> bool | None:
        """``None`` when it could not be measured (not a pass, not a fail)."""
        return None if self.value is None else self.value <= self.limit

    def line(self) -> str:
        shown = "not measured" if self.value is None else f"{self.value:.1f} {self.unit}"
        verdict = {True: "PASS", False: "FAIL", None: "n/a "}[self.ok]
        return f"{verdict}  {self.name}: {shown} (limit {self.limit:g} {self.unit})"


@dataclass(slots=True)
class Report:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(f.ok is not False for f in self.findings)

    def text(self) -> str:
        verdict = "ALL BUDGETS MET" if self.passed else "BUDGET EXCEEDED"
        return "\n".join([*(f.line() for f in self.findings), *self.notes, verdict])

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": {
                f.name: {"value": f.value, "limit": f.limit, "unit": f.unit, "ok": f.ok}
                for f in self.findings
            },
            "notes": self.notes,
        }


# --- the arithmetic ---------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q between 0 and 100) of a non-empty list."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))  # ceiling
    return ordered[int(rank) - 1]


def longest_peak_seconds(samples: Sequence[Sample], limit_pct: float, interval_s: float) -> float:
    """The longest stretch in which the CPU use stayed above ``limit_pct``."""
    longest = run = 0
    for sample in samples:
        if sample.cpu_pct is not None and sample.cpu_pct > limit_pct:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest * interval_s


def summarize(
    samples: Sequence[Sample],
    api_seconds: dict[str, list[float]],
    first_page_s: float | None,
    interval_s: float,
    budgets: Budgets | None = None,
) -> Report:
    """Compare the measurements with the budgets."""
    budgets = budgets or Budgets()
    report = Report()
    memory = [s.rss_mb for s in samples if s.rss_mb is not None]
    cpu = [s.cpu_pct for s in samples if s.cpu_pct is not None]
    warm = memory[len(memory) // 10 :]  # the first tenth is start-up, not steady state
    report.findings.append(
        Finding(
            "memory, steady (median)",
            statistics.median(warm) if warm else None,
            budgets.memory_steady_mb,
            "MB",
        )
    )
    report.findings.append(
        Finding("memory, peak", max(memory) if memory else None, budgets.memory_peak_mb, "MB")
    )
    report.findings.append(
        Finding(
            "CPU, average of one core",
            statistics.fmean(cpu) if cpu else None,
            budgets.cpu_average_pct,
            "%",
        )
    )
    report.findings.append(
        Finding(
            f"CPU, longest time above {budgets.cpu_peak_pct:g}%",
            longest_peak_seconds(samples, budgets.cpu_peak_pct, interval_s) if cpu else None,
            budgets.cpu_peak_seconds - 1e-9,  # "under 5 s": exactly 5 s already fails
            "s",
        )
    )
    every = [ms for times in api_seconds.values() for ms in times]
    report.findings.append(
        Finding(
            "API response, 95th percentile",
            percentile([s * 1000 for s in every], 95) if every else None,
            budgets.api_p95_ms,
            "ms",
        )
    )
    report.findings.append(Finding("first page load", first_page_s, budgets.first_page_s, "s"))
    for path, times in sorted(api_seconds.items()):
        if times:
            ms = [s * 1000 for s in times]
            report.notes.append(
                f"  {path}: {len(ms)} requests, median {statistics.median(ms):.1f} ms, "
                f"95th percentile {percentile(ms, 95):.1f} ms, slowest {max(ms):.1f} ms"
            )
    report.notes.append(
        f"  {len(samples)} samples every {interval_s:g} s"
        + ("" if memory else "; memory and CPU need Linux (/proc)")
    )
    return report


# --- reading the process ----------------------------------------------------------------


def read_rss_mb(pid: int, proc: Path = PROC) -> float | None:
    """Resident memory of a process in MB (Linux)."""
    try:
        fields = (proc / str(pid) / "statm").read_text(encoding="ascii").split()
        return int(fields[1]) * 4096 / (1024 * 1024)  # pages of 4 KB on the Pi
    except (OSError, ValueError, IndexError):
        return None


def read_cpu_seconds(pid: int, proc: Path = PROC) -> float | None:
    """CPU time (user + system) used by a process so far, in seconds (Linux)."""
    try:
        text = (proc / str(pid) / "stat").read_text(encoding="ascii")
        after_name = text.rsplit(")", 1)[1].split()  # the name may contain spaces
        return (int(after_name[11]) + int(after_name[12])) / CLOCK_TICKS
    except (OSError, ValueError, IndexError):
        return None


def find_service_pid() -> int | None:
    """The main process of ``orchestrator.service`` according to systemd."""
    try:
        done = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "orchestrator.service"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    pid = int(done.stdout.strip() or 0)
    return pid or None


# --- the browser clients ----------------------------------------------------------------

Fetch = Callable[[str], float]  # path -> seconds the answer took; raises OSError when none came

UI_PATTERN: tuple[tuple[str, float], ...] = (
    ("/api/status", 10.0),
    ("/api/schedules", 30.0),
    ("/api/system", 60.0),
    ("/api/history", 600.0),
    ("/healthz", 60.0),
)


def http_fetch(host: str, port: int, timeout_s: float = 10.0) -> Fetch:
    def fetch(path: str) -> float:
        started = time.perf_counter()
        connection = http.client.HTTPConnection(host, port, timeout=timeout_s)
        try:
            connection.request("GET", path, headers={"Accept-Encoding": "gzip"})
            answer = connection.getresponse()
            answer.read()
            if answer.status >= 500 and path != "/healthz":
                raise OSError(f"HTTP {answer.status} for {path}")
        except http.client.HTTPException as error:
            raise OSError(str(error)) from None
        finally:
            connection.close()
        return time.perf_counter() - started

    return fetch


class Clients:
    """Plays several open browser pages: each asks for its data on the page's own rhythm."""

    def __init__(
        self,
        fetch: Fetch,
        count: int = 2,
        pattern: Sequence[tuple[str, float]] = UI_PATTERN,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._count = count
        self._pattern = pattern
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self.seconds: dict[str, list[float]] = {}
        self.errors: list[str] = []
        self._threads: list[threading.Thread] = []

    def start(self, stop: threading.Event) -> None:
        for number in range(self._count):
            offset = number * 3.7 % 10 / 10  # the pages are not in step with each other
            thread = threading.Thread(
                target=self._run, args=(stop, offset), name=f"client-{number}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=15)

    def _run(self, stop: threading.Event, offset: float) -> None:
        due = {path: self._monotonic() + offset * every for path, every in self._pattern}
        while not stop.is_set():
            now = self._monotonic()
            for path, every in self._pattern:
                if now >= due[path]:
                    due[path] = now + every
                    self._ask(path)
            stop.wait(min(0.05, max(0.0, min(due.values()) - self._monotonic())))

    def _ask(self, path: str) -> None:
        try:
            took = self._fetch(path)
        except OSError as error:
            with self._lock:
                self.errors.append(f"{path}: {error}")
            return
        with self._lock:
            self.seconds.setdefault(path, []).append(took)


# --- the run ----------------------------------------------------------------------------


def run_check(
    *,
    pid: int | None,
    fetch: Fetch,
    duration_s: float,
    interval_s: float,
    clients: int = 2,
    pattern: Sequence[tuple[str, float]] = UI_PATTERN,
    budgets: Budgets | None = None,
    read_rss: Callable[[int], float | None] = read_rss_mb,
    read_cpu: Callable[[int], float | None] = read_cpu_seconds,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] = lambda text: None,
) -> Report:
    """Measure for ``duration_s`` seconds and return the verdict."""
    try:
        first_page = fetch("/")
    except OSError as error:
        raise SystemExit(f"cannot reach the web page: {error}") from None
    stop = threading.Event()
    players = Clients(fetch, clients, pattern, monotonic)
    players.start(stop)
    samples: list[Sample] = []
    started = monotonic()
    last_cpu = read_cpu(pid) if pid else None
    last_at = started
    next_report = started + 60
    while monotonic() - started < duration_s:
        sleep(interval_s)
        now = monotonic()
        cpu_now = read_cpu(pid) if pid else None
        cpu_pct = None
        if cpu_now is not None and last_cpu is not None and now > last_at:
            cpu_pct = max(0.0, (cpu_now - last_cpu) / (now - last_at) * 100)
        last_cpu, last_at = cpu_now, now
        samples.append(Sample(now - started, read_rss(pid) if pid else None, cpu_pct))
        if now >= next_report:
            next_report += 60
            latest = samples[-1]
            progress(f"{latest.at / 60:.0f} min: memory {latest.rss_mb} MB, cpu {latest.cpu_pct}%")
    stop.set()
    players.join()
    report = summarize(samples, players.seconds, first_page, interval_s, budgets or Budgets())
    if players.errors:
        report.notes.append(f"  {len(players.errors)} failed requests, first: {players.errors[0]}")
        report.findings.append(Finding("failed requests", float(len(players.errors)), 0.0, ""))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--duration", type=float, default=3600.0, help="seconds (default 3600)")
    parser.add_argument("--interval", type=float, default=2.0, help="sampling interval in seconds")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--pid", type=int, help="process id of the service (default: ask systemd)")
    parser.add_argument("--clients", type=int, default=2, help="open browser pages to play")
    parser.add_argument("--json", type=Path, help="also write the result to this file")
    args = parser.parse_args(argv)
    pid = args.pid or find_service_pid()
    if pid is None:
        print("note: no service process found; only response times are measured")
    report = run_check(
        pid=pid,
        fetch=http_fetch(args.host, args.port),
        duration_s=args.duration,
        interval_s=args.interval,
        clients=args.clients,
        progress=print,
    )
    print(report.text())
    if args.json:
        args.json.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
