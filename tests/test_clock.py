"""Clock and time zone resolution (CS-6, CS-13, NFR-4)."""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orchestrator.clock import Clock, SystemClock, resolve_timezone, system_clock
from orchestrator.config import ConfigError, load_config
from tests.fakes import FakeClock

JERUSALEM = ZoneInfo("Asia/Jerusalem")
MISSING = Path("does-not-exist")


def test_configured_zone_wins_over_everything_else(tmp_path: Path) -> None:
    zone_file = tmp_path / "timezone"
    zone_file.write_text("Europe/Paris\n", encoding="utf-8")
    resolved = resolve_timezone(
        "Asia/Jerusalem",
        env={"TZ": "America/New_York"},
        timezone_file=zone_file,
        localtime_link=MISSING,
    )
    assert resolved.tz == JERUSALEM
    assert resolved.source == "configuration"


def test_unknown_configured_zone_is_a_config_error_that_does_not_echo_it() -> None:
    with pytest.raises(ConfigError, match=r"\[general\] timezone") as caught:
        resolve_timezone("Mars/Olympus-Sentinel-4711", env={}, timezone_file=MISSING)
    assert "Sentinel-4711" not in str(caught.value)


def test_tz_environment_variable_is_used_next() -> None:
    resolved = resolve_timezone(
        "", env={"TZ": ":Asia/Jerusalem"}, timezone_file=MISSING, localtime_link=MISSING
    )
    assert resolved.tz == JERUSALEM
    assert resolved.source == "TZ environment variable"


def test_unusable_tz_variable_is_skipped(tmp_path: Path) -> None:
    zone_file = tmp_path / "timezone"
    zone_file.write_text("Asia/Jerusalem\n", encoding="utf-8")
    resolved = resolve_timezone(
        "", env={"TZ": "not/a/zone"}, timezone_file=zone_file, localtime_link=MISSING
    )
    assert resolved.tz == JERUSALEM
    assert resolved.source == str(zone_file)


def test_localtime_link_target_names_the_zone(tmp_path: Path) -> None:
    # a regular file in a ".../zoneinfo/<Area>/<City>" path stands in for the /etc/localtime link
    target = tmp_path / "usr" / "share" / "zoneinfo" / "Asia" / "Jerusalem"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    resolved = resolve_timezone("", env={}, timezone_file=MISSING, localtime_link=target)
    assert resolved.tz == JERUSALEM


def test_last_resort_is_the_operating_system_offset() -> None:
    resolved = resolve_timezone("", env={}, timezone_file=MISSING, localtime_link=MISSING)
    assert datetime.now(resolved.tz).utcoffset() is not None
    assert "no DST rules" in resolved.source


def test_system_clock_is_timezone_aware_and_in_the_configured_zone() -> None:
    clock: Clock = SystemClock(JERUSALEM)
    now = clock.now()
    assert now.tzinfo == JERUSALEM
    assert abs(now.timestamp() - datetime.now().timestamp()) < 5
    assert clock.monotonic() <= clock.monotonic()


def test_system_clock_is_built_from_the_config_timezone(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[general]\ntimezone = "Asia/Jerusalem"\n', encoding="utf-8")
    clock = system_clock(load_config(config_file, env={}), env={})
    assert clock.tz == JERUSALEM


def test_config_rejects_an_unknown_timezone(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[general]\ntimezone = "Mars/Olympus-Sentinel-4711"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[general\] timezone") as caught:
        load_config(config_file, env={})
    assert "Sentinel-4711" not in str(caught.value)


def test_fake_clock_moves_only_when_told() -> None:
    clock = FakeClock(datetime(2026, 10, 24, 12, 0, tzinfo=JERUSALEM))
    before_monotonic = clock.monotonic()
    assert clock.now() == clock.now()
    clock.advance(timedelta(minutes=90))
    assert clock.now() == datetime(2026, 10, 24, 13, 30, tzinfo=JERUSALEM)
    assert clock.monotonic() == before_monotonic + 5400


def test_fake_clock_follows_the_spring_daylight_saving_change() -> None:
    # Israel moves clocks forward on Friday 2026-03-27 at 02:00 (+02:00 -> +03:00)
    clock = FakeClock(datetime(2026, 3, 26, 23, 0, tzinfo=JERUSALEM))
    assert clock.now().utcoffset() == timedelta(hours=2)
    clock.advance(timedelta(hours=24))
    assert clock.now() == datetime(2026, 3, 28, 0, 0, tzinfo=JERUSALEM)
    assert clock.now().utcoffset() == timedelta(hours=3)


def test_fake_clock_follows_the_autumn_daylight_saving_change() -> None:
    # clocks go back on Sunday 2026-10-25 at 02:00 (+03:00 -> +02:00)
    clock = FakeClock(datetime(2026, 10, 24, 23, 0, tzinfo=JERUSALEM))
    assert clock.now().utcoffset() == timedelta(hours=3)
    clock.advance(timedelta(hours=24))
    assert clock.now() == datetime(2026, 10, 25, 22, 0, tzinfo=JERUSALEM)
    assert clock.now().utcoffset() == timedelta(hours=2)


def test_fake_clock_refuses_a_naive_start_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 10, 24, 12, 0))
