"""Sun and moon times (FR-CFG-6a): daylight saving, the legacy week, errors, the location."""

import dataclasses
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from astral import Observer
from astral.moon import moonrise

from orchestrator.astro import (
    AstroError,
    Location,
    load_location,
    resolve_astro,
    save_location,
)
from orchestrator.config import Config, ConfigError, LocationConfig, load_config
from orchestrator.event_titles import AstroTime
from orchestrator.schedule_files import parse_timer_schedule
from orchestrator.weekdays import boiler_day

ROOT = Path(__file__).resolve().parent.parent
JERUSALEM = ZoneInfo("Asia/Jerusalem")
RAMAT_GAN = Location(32.07, 34.82)  # approximate; the legacy code geocoded a street address


def seconds_of_day(moment: datetime) -> int:
    return moment.hour * 3600 + moment.minute * 60 + moment.second


# --- the real legacy output, reproduced -------------------------------------------------------


def test_the_legacy_timecal_json_is_reproduced_by_sunset_minus_30_minutes() -> None:
    """Every ON time of the real TimeCal.json (week of Sunday 2026-10-18) to within 2 seconds."""
    schedule = parse_timer_schedule((ROOT / "TimeCal.json").read_bytes())
    on_times = {e.day: e.at for e in schedule.entries if e.state == 1}
    assert sorted(on_times) == [1, 2, 3, 4, 5, 6, 7]
    for offset in range(7):
        day = date(2026, 10, 18) + timedelta(days=offset)
        moment = resolve_astro(AstroTime("sunset", -30), day, RAMAT_GAN, JERUSALEM)
        legacy = on_times[boiler_day(day)]
        legacy_seconds = legacy.hour * 3600 + legacy.minute * 60 + legacy.second
        assert abs(seconds_of_day(moment) - legacy_seconds) <= 2, (day, moment, legacy)


# --- daylight saving follows the event date ---------------------------------------------------


def test_the_utc_offset_is_the_one_of_the_event_date_not_of_today() -> None:
    before = resolve_astro(AstroTime("sunset"), date(2026, 10, 24), RAMAT_GAN, JERUSALEM)
    after = resolve_astro(AstroTime("sunset"), date(2026, 10, 25), RAMAT_GAN, JERUSALEM)
    assert before.utcoffset() == timedelta(hours=3)  # summer time until Sunday 2026-10-25 02:00
    assert after.utcoffset() == timedelta(hours=2)
    # the wall-clock sunset jumps back by about an hour, the real moment moves by about 24 h
    assert time(17, 50) <= before.time() <= time(18, 10)
    assert time(16, 50) <= after.time() <= time(17, 10)
    gap = after.astimezone(UTC) - before.astimezone(UTC)  # same-zone subtraction is wall-clock
    assert timedelta(hours=23, minutes=55) < gap < timedelta(hours=24, minutes=5)


def test_the_result_is_aware_in_the_given_zone_and_in_whole_seconds() -> None:
    moment = resolve_astro(AstroTime("sunrise", 7), date(2026, 3, 1), RAMAT_GAN, JERUSALEM)
    assert moment.tzinfo is JERUSALEM
    assert moment.microsecond == 0


def test_the_same_inputs_always_give_the_same_moment() -> None:
    args = (AstroTime("dusk", 5), date(2026, 6, 21), RAMAT_GAN, JERUSALEM)
    assert resolve_astro(*args) == resolve_astro(*args)


# --- offsets ----------------------------------------------------------------------------------


@pytest.mark.parametrize("minutes", [-90, -30, 0, 1, 30, 600])
def test_offsets_are_added_in_minutes(minutes: int) -> None:
    base = resolve_astro(AstroTime("sunset"), date(2026, 10, 20), RAMAT_GAN, JERUSALEM)
    moved = resolve_astro(AstroTime("sunset", minutes), date(2026, 10, 20), RAMAT_GAN, JERUSALEM)
    assert moved - base == timedelta(minutes=minutes)


def test_a_negative_offset_can_reach_the_previous_date() -> None:
    # solar midnight of a date is just after 00:00 local time; 30 minutes earlier is the day before
    moment = resolve_astro(AstroTime("midnight", -30), date(2026, 10, 20), RAMAT_GAN, JERUSALEM)
    assert moment.date() == date(2026, 10, 19)


# --- every name -------------------------------------------------------------------------------


def test_the_names_come_in_the_expected_order_on_an_ordinary_day() -> None:
    day = date(2026, 10, 20)

    def at(name: str) -> datetime:
        return resolve_astro(AstroTime(name), day, RAMAT_GAN, JERUSALEM)

    assert at("dawn") < at("sunrise") < at("noon") < at("sunset") < at("dusk")
    assert at("twilight") == at("dusk")  # twilight means civil dusk
    assert at("midnight").time() < time(2, 0)  # the solar midnight that falls on that date
    assert at("moonrise").tzinfo is JERUSALEM and at("moonset").tzinfo is JERUSALEM


# --- things that cannot be computed -----------------------------------------------------------


def test_without_a_location_the_event_is_reported_not_guessed() -> None:
    with pytest.raises(AstroError, match="no location is configured"):
        resolve_astro(AstroTime("sunset"), date(2026, 10, 20), None, JERUSALEM)


def test_an_unknown_name_is_an_error() -> None:
    with pytest.raises(AstroError, match="not a known"):
        resolve_astro(AstroTime("teatime"), date(2026, 10, 20), RAMAT_GAN, JERUSALEM)


def test_the_midnight_sun_has_no_sunset() -> None:
    far_north = Location(80.0, 20.0)
    with pytest.raises(AstroError, match="sunset does not occur"):
        resolve_astro(AstroTime("sunset"), date(2026, 6, 21), far_north, JERUSALEM)


def test_a_day_without_a_moonrise_is_an_error_not_a_crash() -> None:
    observer = Observer(RAMAT_GAN.latitude, RAMAT_GAN.longitude)
    missing: date | None = None
    for offset in range(40):  # about once a month the moon does not rise on a calendar date
        day = date(2026, 10, 1) + timedelta(days=offset)
        try:
            moonrise(observer, date=day, tzinfo=JERUSALEM)
        except ValueError:
            missing = day
            break
    assert missing is not None, "expected a day without a moonrise within 40 days"
    with pytest.raises(AstroError, match="moonrise does not occur"):
        resolve_astro(AstroTime("moonrise"), missing, RAMAT_GAN, JERUSALEM)


@pytest.mark.parametrize(("latitude", "longitude"), [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_a_location_outside_the_globe_is_refused(latitude: float, longitude: float) -> None:
    with pytest.raises(AstroError, match="latitude must be"):
        Location(latitude, longitude)


# --- where the location comes from ------------------------------------------------------------


def make_config(tmp_path: Path, location_toml: str = "") -> Config:
    path = tmp_path / "config.toml"
    data_dir = (tmp_path / "data").as_posix()
    path.write_text(f'[general]\ndata_dir = "{data_dir}"\n{location_toml}', encoding="utf-8")
    return load_config(path, env={})


def test_no_location_anywhere_gives_none(tmp_path: Path) -> None:
    assert load_location(make_config(tmp_path)) is None


def test_the_configured_location_is_used(tmp_path: Path) -> None:
    config = make_config(tmp_path, "[location]\nlatitude = 32.07\nlongitude = 34.82\n")
    assert load_location(config) == RAMAT_GAN


def test_the_cache_is_used_when_nothing_is_configured(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert save_location(Path(config.general.data_dir), Location(31.5, 35.0)) is True
    assert load_location(config) == Location(31.5, 35.0)
    assert save_location(Path(config.general.data_dir), Location(31.5, 35.0)) is False  # unchanged


def test_the_configured_location_wins_over_the_cache(tmp_path: Path) -> None:
    config = make_config(tmp_path, "[location]\nlatitude = 32.07\nlongitude = 34.82\n")
    save_location(Path(config.general.data_dir), Location(1.0, 2.0))
    assert load_location(config) == RAMAT_GAN


@pytest.mark.parametrize(
    "content",
    [
        "{ not json",
        "[]",
        '{"latitude": 1}',
        '{"latitude": 95, "longitude": 0}',
        '{"latitude": "x", "longitude": 1}',
    ],
)
def test_an_unreadable_cache_is_ignored_with_a_warning(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_config(tmp_path)
    cache = Path(config.general.data_dir)
    cache.mkdir(parents=True)
    (cache / "location.json").write_text(content, encoding="utf-8")
    assert load_location(config) is None
    assert "location_cache_unreadable" in caplog.text


def test_the_cache_file_is_plain_json(tmp_path: Path) -> None:
    save_location(tmp_path, Location(32.07, 34.82))
    assert json.loads((tmp_path / "location.json").read_text(encoding="utf-8")) == {
        "latitude": 32.07,
        "longitude": 34.82,
    }


def test_the_config_defaults_to_no_location() -> None:
    assert load_config(None, env={}).location == LocationConfig()


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("[location]\nlatitude = 32.0\n", "latitude and longitude must be given together"),
        ("[location]\nlongitude = 34.0\n", "latitude and longitude must be given together"),
        ("[location]\nlatitude = 95\nlongitude = 0\n", r"\[location\] latitude"),
        ("[location]\nlatitude = 0\nlongitude = 200\n", r"\[location\] longitude"),
        ('[location]\nlatitude = "north"\nlongitude = 0\n', r"\[location\] latitude"),
    ],
)
def test_bad_location_settings_are_rejected(tmp_path: Path, toml: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path, env={})


def test_a_frozen_location_cannot_be_changed() -> None:
    location = Location(1.0, 2.0)
    field_name = "latitude"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(location, field_name, 5.0)
