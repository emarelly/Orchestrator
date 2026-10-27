"""Manual On / Off / Auto and relay-run files (FR-MAN-1..7, FR-LEG-6)."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orchestrator.config import LimitsConfig
from orchestrator.manual import (
    MAX_MANUAL_HOURS,
    MIN_MANUAL_HOURS,
    ManualError,
    build_auto,
    build_manual_off,
    build_manual_on,
    build_relay_run,
    manual_duration,
    manual_on_level,
    manual_upload,
    relay_filename,
    relay_off_upload,
    relay_run_upload,
)
from orchestrator.models import BoilerStatus, ManualEntry, ModelError, TimerEntry
from orchestrator.schedule_files import parse_manual
from orchestrator.validation import ValidationError

JERUSALEM = ZoneInfo("Asia/Jerusalem")
SATURDAY_4PM = datetime(2026, 10, 24, 16, 0, 0, tzinfo=JERUSALEM)  # the manual.json example day
LIMITS = LimitsConfig()  # max 5, margin 1.0, duration 3 h

SAMPLE: dict[str, Any] = {
    "ActiveYesNo": "Yes",
    "CurrentShowers": 3.43,
    "CurrentShowersnew": 4.92,
    "CurrentTemp": [58.06, 54.19, 61.44],
    "Date": "2026-10-24T16:40:19.707843",
    "HeaterOnOff": "Off",
    "MinVal": 4.0,
    "Status": "Manual",
    "TargetVal": 4.0,
    "TimeToStartMin": 175,
}


def status(linear: float, thermo: float) -> BoilerStatus:
    return BoilerStatus.parse({**SAMPLE, "CurrentShowers": linear, "CurrentShowersnew": thermo})


# --- manual On --------------------------------------------------------------------------------


def test_on_is_capped_at_5_and_expires_in_three_hours() -> None:
    # 4.92 + 1.0 = 5.92 would be above the heater limit
    override = build_manual_on(BoilerStatus.parse(SAMPLE), SATURDAY_4PM, LIMITS)
    assert override.entry == ManualEntry(7, time(19, 0, 0), Decimal("5.0"), Decimal("5.0"))
    assert manual_upload(override).content == (
        b'[{"DayOfWeek":7,"Hour":"19:00:00","Target":5.0,"Min":5.0}]'
    )


@pytest.mark.parametrize(
    ("linear", "thermo", "expected"),
    [
        (2.31, 2.05, "3.4"),  # 2.31 + 1.0 = 3.31 -> rounded up to 3.4
        (1.0, 3.01, "4.1"),  # the larger value is used: 3.01 + 1.0 = 4.01 -> 4.1
        (3.2, 0.5, "4.2"),
        (2.3, 2.3, "3.3"),  # no binary floating point error: exactly 3.3, not 3.4
        (0.0, 0.0, "1.0"),
        (4.0, 4.0, "5.0"),  # 5.0 exactly reaches the cap
    ],
)
def test_on_level_is_current_plus_margin_rounded_up(
    linear: float, thermo: float, expected: str
) -> None:
    assert str(manual_on_level(status(linear, thermo), LIMITS)) == expected


@pytest.mark.parametrize("current", [4.0, 4.5, 4.99, 5.0, 5.5, 8.0, 10.0])
def test_on_level_never_exceeds_the_cap_and_equals_it_when_already_full(current: float) -> None:
    level = manual_on_level(status(current, current), LIMITS)
    assert level == Decimal("5.0")


def test_on_level_is_always_above_the_current_showers_below_the_cap() -> None:
    for tenths in range(0, 40):  # 0.0 .. 3.9
        current = tenths / 10
        level = manual_on_level(status(current, current), LIMITS)
        assert Decimal(str(current)) < level <= Decimal("5.0"), current


def test_a_lower_configured_limit_is_respected() -> None:
    limits = LimitsConfig(max_showers=3.5)
    assert manual_on_level(status(2.0, 3.0), limits) == Decimal("3.5")


def test_the_margin_is_configurable() -> None:
    limits = LimitsConfig(manual_margin=0.5)
    assert manual_on_level(status(2.0, 2.0), limits) == Decimal("2.5")


def test_on_needs_a_known_status() -> None:
    with pytest.raises(ManualError, match="status is unknown"):
        build_manual_on(None, SATURDAY_4PM, LIMITS)


def test_on_refuses_the_not_yet_measured_placeholder_value() -> None:
    with pytest.raises(ManualError, match="valid number of showers"):
        build_manual_on(status(-1.0, -1.0), SATURDAY_4PM, LIMITS)


# --- manual Off and Auto ----------------------------------------------------------------------


def test_off_is_zero_zero_with_the_same_expiry() -> None:
    override = build_manual_off(SATURDAY_4PM, LIMITS)
    assert override.entry == ManualEntry(7, time(19, 0, 0), Decimal(0), Decimal(0))
    assert manual_upload(override).content == (
        b'[{"DayOfWeek":7,"Hour":"19:00:00","Target":0,"Min":0}]'
    )


def test_auto_is_the_empty_file() -> None:
    upload = manual_upload(build_auto())
    assert (upload.filename, upload.content) == ("manual.json", b"[]")
    assert parse_manual(upload.content).is_auto


def test_the_upload_name_of_the_boiler_override_is_manual_json() -> None:
    assert manual_upload(build_manual_off(SATURDAY_4PM, LIMITS)).filename == "manual.json"


# --- expiry -----------------------------------------------------------------------------------


def test_the_duration_is_configurable() -> None:
    limits = LimitsConfig(manual_duration_hours=1.5)
    entry = build_manual_off(SATURDAY_4PM, limits).entry
    assert entry is not None
    assert (entry.expires_day, entry.expires_at) == (7, time(17, 30, 0))


def test_expiry_rolls_over_from_saturday_to_sunday() -> None:
    saturday_night = datetime(2026, 10, 31, 23, 0, tzinfo=JERUSALEM)
    entry = build_manual_off(saturday_night, LIMITS).entry
    assert entry is not None
    assert (entry.expires_day, entry.expires_at) == (1, time(2, 0, 0))


def test_expiry_follows_the_clock_change_in_october() -> None:
    saturday_night = datetime(2026, 10, 24, 23, 0, tzinfo=JERUSALEM)  # clocks go back at 02:00
    entry = build_manual_off(saturday_night, LIMITS).entry
    assert entry is not None
    assert (entry.expires_day, entry.expires_at) == (1, time(1, 0, 0))


def test_a_naive_time_is_refused() -> None:
    naive = datetime(2026, 10, 24, 16, 0, 0)
    with pytest.raises(ModelError, match="timezone-aware"):
        build_manual_off(naive, LIMITS)
    with pytest.raises(ModelError, match="timezone-aware"):
        build_manual_on(BoilerStatus.parse(SAMPLE), naive, LIMITS)


# --- relay runs -------------------------------------------------------------------------------


def test_relay_run_matches_the_documented_example_exactly() -> None:
    friday = datetime(2026, 10, 23, 13, 25, 9, 400_000, tzinfo=JERUSALEM)
    upload = relay_run_upload(2, 150, friday)
    assert upload.filename == "2Timermanual.json"
    assert upload.content == (
        b'[{"DayOfWeek":6,"Hour":"13:25:09","State":1,"RelayNum":2},'
        b'{"DayOfWeek":6,"Hour":"13:27:39","State":0,"RelayNum":2}]'
    )


def test_relay_run_across_midnight_between_saturday_and_sunday() -> None:
    saturday = datetime(2026, 10, 31, 23, 59, 30, tzinfo=JERUSALEM)
    on, off = build_relay_run(5, 60, saturday)
    assert on == TimerEntry(7, time(23, 59, 30), 1, 5)
    assert off == TimerEntry(1, time(0, 0, 30), 0, 5)
    # the file lists entries in week order, so Sunday's OFF comes before Saturday's ON
    assert relay_run_upload(5, 60, saturday).content == (
        b'[{"DayOfWeek":1,"Hour":"00:00:30","State":0,"RelayNum":5},'
        b'{"DayOfWeek":7,"Hour":"23:59:30","State":1,"RelayNum":5}]'
    )


@pytest.mark.parametrize("relay", [0, 1, 8, -1])
def test_relays_0_and_1_and_unknown_relays_are_never_switched(relay: int) -> None:
    with pytest.raises(ValidationError, match="cannot be switched manually"):
        relay_run_upload(relay, 600, SATURDAY_4PM)
    with pytest.raises(ValidationError, match="cannot be switched manually"):
        relay_off_upload(relay, SATURDAY_4PM)


@pytest.mark.parametrize("seconds", [0, 10, 86_401])
def test_relay_duration_limits_are_enforced(seconds: int) -> None:
    with pytest.raises(ValidationError, match="duration"):
        relay_run_upload(4, seconds, SATURDAY_4PM)


def test_relay_off_file_has_a_single_off_entry_now() -> None:
    upload = relay_off_upload(4, SATURDAY_4PM)
    assert upload.filename == "4Timermanual.json"
    assert upload.content == b'[{"DayOfWeek":7,"Hour":"16:00:00","State":0,"RelayNum":4}]'


def test_relay_file_name() -> None:
    assert relay_filename(2) == "2Timermanual.json"


def test_relay_run_with_a_naive_time_is_refused() -> None:
    with pytest.raises(ModelError, match="timezone-aware"):
        relay_run_upload(4, 600, datetime(2026, 10, 24, 16, 0, 0))
    with pytest.raises(ManualError, match="timezone-aware"):
        relay_off_upload(4, datetime(2026, 10, 24, 16, 0, 0))


def test_a_channel_may_ask_for_a_shorter_or_longer_override_within_limits() -> None:
    limits = LimitsConfig()
    assert manual_duration(limits, None) == timedelta(hours=3)
    assert manual_duration(limits, 2) == timedelta(hours=2)
    assert manual_duration(limits, MIN_MANUAL_HOURS) == timedelta(minutes=15)
    assert manual_duration(limits, MAX_MANUAL_HOURS) == timedelta(hours=12)
    unacceptable: list[Any] = [0, -1, 0.1, 12.01, 24, True, float("nan"), "3"]
    for bad in unacceptable:
        with pytest.raises(ManualError, match="duration"):
            manual_duration(limits, bad)
