"""The performance check tool (NFR-1a): its arithmetic, its readers and a short real run."""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.test_app import Harness
from tools import perf_check
from tools.perf_check import (
    Budgets,
    Finding,
    Sample,
    find_service_pid,
    http_fetch,
    longest_peak_seconds,
    main,
    percentile,
    read_cpu_seconds,
    read_rss_mb,
    run_check,
    summarize,
)


def samples(memory: Sequence[float | None], cpu: Sequence[float | None]) -> list[Sample]:
    return [Sample(i * 2.0, m, c) for i, (m, c) in enumerate(zip(memory, cpu, strict=True))]


def finding(report: perf_check.Report, start: str) -> Finding:
    return next(f for f in report.findings if f.name.startswith(start))


# --- the arithmetic ---------------------------------------------------------------------


def test_percentiles_use_the_nearest_rank_whatever_the_order() -> None:
    values = [float(n) for n in range(100, 0, -1)]
    assert percentile(values, 95) == 95.0
    assert percentile(values, 50) == 50.0
    assert percentile(values, 100) == 100.0
    assert percentile([7.0], 95) == 7.0
    assert percentile([1.0, 2.0], 95) == 2.0


def test_the_longest_stretch_above_the_limit_is_measured_in_seconds() -> None:
    cpu = [10, 60, 70, 20, 80, 90, 95, 5]
    items = samples([1.0] * 8, cpu)
    assert longest_peak_seconds(items, 50, 2.0) == 6.0  # three samples in a row, 2 s apart
    assert longest_peak_seconds(items, 99, 2.0) == 0.0
    assert longest_peak_seconds(samples([1.0], [None]), 50, 2.0) == 0.0


GOOD_API = {"/api/status": [0.004, 0.006, 0.005], "/api/schedules": [0.012, 0.01]}


def good_run() -> list[Sample]:
    return samples([60.0 + n * 0.1 for n in range(50)], [2.0] * 50)


def test_a_run_inside_every_budget_passes() -> None:
    report = summarize(good_run(), GOOD_API, 0.4, 2.0)
    assert report.passed
    assert all(f.ok is True for f in report.findings)
    assert "ALL BUDGETS MET" in report.text() and "PASS" in report.text()


def test_the_first_tenth_is_start_up_and_does_not_count_as_steady_state() -> None:
    memory = [200.0] * 5 + [90.0] * 45  # a high start-up, a calm run
    report = summarize(samples(memory, [1.0] * 50), GOOD_API, 0.4, 2.0)
    assert finding(report, "memory, steady").value == 90.0
    assert finding(report, "memory, steady").ok is True
    assert finding(report, "memory, peak").ok is False  # but the peak above 150 MB is reported
    assert not report.passed


@pytest.mark.parametrize(
    ("memory", "cpu", "api", "page", "failing"),
    [
        ([130.0] * 50, [2.0] * 50, GOOD_API, 0.4, "memory, steady"),
        ([60.0] * 50, [6.0] * 50, GOOD_API, 0.4, "CPU, average"),
        ([60.0] * 50, [1.0] * 47 + [60.0] * 3, GOOD_API, 0.4, "CPU, longest"),
        ([60.0] * 50, [2.0] * 50, {"/api/status": [0.2] * 30}, 0.4, "API response"),
        ([60.0] * 50, [2.0] * 50, GOOD_API, 3.0, "first page load"),
    ],
)
def test_each_budget_can_fail_on_its_own(
    memory: list[float], cpu: list[float], api: dict[str, list[float]], page: float, failing: str
) -> None:
    report = summarize(samples(list(memory), list(cpu)), api, page, 2.0)
    assert next(f.name for f in report.findings if f.ok is False).startswith(failing)
    assert not report.passed and "BUDGET EXCEEDED" in report.text()


def test_exactly_five_seconds_above_the_peak_limit_already_fails() -> None:
    items = samples([60.0] * 20, [1.0] * 17 + [80.0] * 3)  # 3 samples x 2 s = 6 s
    assert finding(summarize(items, GOOD_API, 0.4, 2.0), "CPU, longest").ok is False
    short = samples([60.0] * 20, [1.0] * 18 + [80.0] * 2)  # 4 s
    assert finding(summarize(short, GOOD_API, 0.4, 2.0), "CPU, longest").ok is True


def test_what_cannot_be_measured_is_shown_as_such_and_is_not_a_failure() -> None:
    report = summarize(samples([None] * 5, [None] * 5), GOOD_API, 0.4, 2.0)
    assert finding(report, "memory, steady").ok is None
    assert report.passed
    assert "not measured" in report.text() and "need Linux" in report.text()


def test_the_result_can_be_written_as_json() -> None:
    data = summarize(good_run(), GOOD_API, 0.4, 2.0).as_dict()
    assert data["passed"] is True
    assert data["findings"]["API response, 95th percentile"]["limit"] == Budgets().api_p95_ms


# --- reading the process ----------------------------------------------------------------


def test_memory_is_read_from_the_page_count(tmp_path: Path) -> None:
    (tmp_path / "42").mkdir()
    (tmp_path / "42" / "statm").write_text("30000 25600 400 10 0 5000 0\n", encoding="ascii")
    assert read_rss_mb(42, tmp_path) == 100.0  # 25600 pages of 4 KB
    assert read_rss_mb(43, tmp_path) is None
    (tmp_path / "44").mkdir()
    (tmp_path / "44" / "statm").write_text("garbage", encoding="ascii")
    assert read_rss_mb(44, tmp_path) is None


def test_cpu_time_survives_a_process_name_with_spaces_and_brackets(tmp_path: Path) -> None:
    after_name = "S 1 42 42 0 -1 4194304 5000 0 0 0 250 50 0 0 20 0 3 0 12345 1000000 25600"
    (tmp_path / "42").mkdir()
    (tmp_path / "42" / "stat").write_text(f"42 (py (thon) 3) {after_name}\n", encoding="ascii")
    assert read_cpu_seconds(42, tmp_path) == 3.0  # (250 + 50) ticks of 1/100 s
    assert read_cpu_seconds(7, tmp_path) is None


def test_the_service_process_is_asked_from_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(output: str) -> Any:
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake("1234\n"))
    assert find_service_pid() == 1234
    monkeypatch.setattr(subprocess, "run", fake("0\n"))
    assert find_service_pid() is None  # the service is not running

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("no systemctl")

    monkeypatch.setattr(subprocess, "run", missing)
    assert find_service_pid() is None


# --- the run, with a fake process and fake web server -----------------------------------


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_run_counts_cpu_from_the_process_time_and_reports_the_samples() -> None:
    fake = FakeTime()
    cpu_total = {"value": 0.0}

    def read_cpu(pid: int) -> float:
        cpu_total["value"] += 0.04  # 0.04 s of CPU in every 2 s = 2 %
        return cpu_total["value"]

    report = run_check(
        pid=99,
        fetch=lambda path: 0.005,
        duration_s=20,
        interval_s=2,
        clients=1,
        pattern=(("/api/status", 1000.0),),
        read_rss=lambda pid: 70.0,
        read_cpu=read_cpu,
        monotonic=fake.monotonic,
        sleep=fake.sleep,
    )
    cpu = finding(report, "CPU, average")
    assert cpu.value is not None and abs(cpu.value - 2.0) < 0.2
    assert finding(report, "memory, steady").value == 70.0
    assert "10 samples every 2 s" in report.text()


def test_failed_requests_are_a_failure() -> None:
    def fetch(path: str) -> float:
        if path == "/api/system":
            raise OSError("connection refused")
        return 0.001

    report = run_check(
        pid=None,
        fetch=fetch,
        duration_s=0.6,
        interval_s=0.1,
        clients=1,
        pattern=(("/api/system", 0.05), ("/api/status", 0.05)),
    )
    assert finding(report, "failed requests").ok is False and not report.passed
    assert "connection refused" in report.text()


def test_an_unreachable_service_stops_the_check_with_a_clear_message() -> None:
    def fetch(path: str) -> float:
        raise OSError("refused")

    with pytest.raises(SystemExit, match="cannot reach the web page"):
        run_check(pid=None, fetch=fetch, duration_s=1, interval_s=0.1)


# --- against the real service (started on fakes) ----------------------------------------


def test_the_two_clients_ask_the_running_service_the_way_the_page_does(tmp_path: Path) -> None:
    made = Harness(tmp_path).start()
    try:
        report = run_check(
            pid=None,
            fetch=http_fetch("127.0.0.1", made.app.web_port),
            duration_s=1.5,
            interval_s=0.25,
            clients=2,
            pattern=(
                ("/api/status", 0.1),
                ("/api/schedules", 0.2),
                ("/api/system", 0.3),
                ("/api/history", 0.5),
                ("/healthz", 0.3),
            ),
        )
    finally:
        made.stop()
    text = report.text()
    for path in ("/api/status", "/api/schedules", "/api/system", "/api/history"):
        assert path in text, path
    assert finding(report, "failed requests") if "failed requests" in text else True
    api = finding(report, "API response")
    assert api.value is not None and api.limit == 100.0
    first = finding(report, "first page load")
    assert first.value is not None and first.value < 5


def test_the_command_line_writes_the_report_and_a_json_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    made = Harness(tmp_path).start()
    out = tmp_path / "perf.json"
    try:
        code = main(
            [
                "--port",
                str(made.app.web_port),
                "--duration",
                "1",
                "--interval",
                "0.25",
                "--pid",
                "999999",
                "--json",
                str(out),
            ]
        )
    finally:
        made.stop()
    printed = capsys.readouterr().out
    assert code in (0, 1)
    assert "API response, 95th percentile" in printed
    data = json.loads(out.read_text(encoding="utf-8"))
    assert {"passed", "findings", "notes"} <= set(data)
