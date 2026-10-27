"""Schedule file writer and reader (FR-PUSH-7, CS-14, OQ-17 evidence)."""

import json
import random
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from orchestrator.models import (
    BoilerSchedule,
    BoilerSlot,
    ManualEntry,
    ManualOverride,
    TimerEntry,
    TimerSchedule,
)
from orchestrator.schedule_files import (
    ScheduleFileError,
    parse_boiler_schedule,
    parse_manual,
    parse_timer_schedule,
    serialize_boiler_schedule,
    serialize_manual,
    serialize_relay_manual,
    serialize_timer_schedule,
)
from tests import legacy_reader

ROOT = Path(__file__).resolve().parent.parent
CAL = (ROOT / "Cal.json").read_bytes()
TIME_CAL = (ROOT / "TimeCal.json").read_bytes()
MANUAL = (ROOT / "ref files" / "manual.json").read_bytes()
# the relay manual example from the requirements (relay 2, 150 seconds on a Friday)
RELAY_MANUAL = (
    b'[{"DayOfWeek":6,"Hour":"13:25:09","State":1,"RelayNum":2},'
    b'{"DayOfWeek":6,"Hour":"13:27:39","State":0,"RelayNum":2}]'
)


# --- golden files: byte for byte --------------------------------------------------------------


def test_cal_json_round_trips_byte_for_byte() -> None:
    assert serialize_boiler_schedule(parse_boiler_schedule(CAL)) == CAL


def test_time_cal_json_round_trips_byte_for_byte() -> None:
    assert serialize_timer_schedule(parse_timer_schedule(TIME_CAL)) == TIME_CAL


def test_the_sample_manual_json_round_trips_byte_for_byte() -> None:
    override = parse_manual(MANUAL)
    assert override.entry == ManualEntry(7, time(19, 0, 0), Decimal("4"), Decimal("4"))
    assert serialize_manual(override) == MANUAL


def test_the_relay_manual_example_is_written_exactly() -> None:
    on = TimerEntry(6, time(13, 25, 9), 1, 2)
    off = TimerEntry(6, time(13, 27, 39), 0, 2)
    assert serialize_relay_manual([off, on]) == RELAY_MANUAL  # order of input does not matter
    assert serialize_timer_schedule(parse_timer_schedule(RELAY_MANUAL)) == RELAY_MANUAL


def test_the_golden_files_are_exactly_what_the_legacy_writer_produces() -> None:
    cal = CAL.decode("ascii")
    time_cal = TIME_CAL.decode("ascii")
    assert legacy_reader.write_boiler(legacy_reader.parse(cal)) == cal
    assert legacy_reader.write_timer(legacy_reader.parse(time_cal)) == time_cal


def test_the_mixed_number_spellings_of_cal_json_are_preserved() -> None:
    text = serialize_boiler_schedule(parse_boiler_schedule(CAL)).decode("ascii")
    assert '"Target":3.0,"Min":3}' in text  # the one Min written without a decimal point
    assert '"Target":3.6,"Min":2.0}' in text


# --- the legacy reader must understand what we write ------------------------------------------


def test_the_legacy_reader_reads_our_boiler_file_like_the_original() -> None:
    ours = legacy_reader.parse(serialize_boiler_schedule(parse_boiler_schedule(CAL)).decode())
    assert ours == legacy_reader.parse(CAL.decode())
    assert len(ours) == len(parse_boiler_schedule(CAL))


def test_the_legacy_reader_reads_our_timer_file_like_the_original() -> None:
    ours = legacy_reader.parse(serialize_timer_schedule(parse_timer_schedule(TIME_CAL)).decode())
    assert ours == legacy_reader.parse(TIME_CAL.decode())


def test_the_legacy_reader_reads_generated_schedules_back_exactly() -> None:
    rng = random.Random(20261024)
    slots = [
        BoilerSlot.create(
            rng.randint(1, 7),
            time(rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59)),
            f"{rng.randint(0, 5)}.{rng.randint(0, 9)}",
            rng.choice(["2", "2.5", "3.0", "0"]),
        )
        for _ in range(60)
    ]
    schedule = BoilerSchedule.of(slots)
    parsed = legacy_reader.parse(serialize_boiler_schedule(schedule).decode())
    expected = [
        {
            "day": s.day,
            "hour": s.start.strftime("%H:%M:%S"),
            "a": format(s.target, "f"),
            "b": format(s.minimum, "f"),
        }
        for s in schedule.slots
    ]
    assert parsed == expected
    assert parse_boiler_schedule(serialize_boiler_schedule(schedule)) == schedule


def test_pretty_printed_json_would_break_the_legacy_reader() -> None:
    """Why the exact format matters: the same data, indented, is misread by the services."""
    data = json.loads(CAL)
    pretty = json.dumps(data, indent=2)
    try:
        misread = legacy_reader.parse(pretty)
    except (ValueError, IndexError):
        return  # it even crashes: also proves the point
    assert misread != legacy_reader.parse(CAL.decode())


# --- format properties ------------------------------------------------------------------------


@pytest.mark.parametrize("data", [CAL, TIME_CAL, MANUAL, RELAY_MANUAL])
def test_golden_output_is_compact_ascii_without_bom_or_newline(data: bytes) -> None:
    assert data[:1] == b"[" and data[-1:] == b"]"
    assert not any(byte in data for byte in b" \r\n\t")
    assert not data.startswith(b"\xef\xbb\xbf")


def test_output_is_sorted_by_day_and_time_with_the_fixed_key_order() -> None:
    schedule = BoilerSchedule.of(
        [
            BoilerSlot.create(2, "06:00:00", 4, 2),
            BoilerSlot.create(1, "18:00:00", "3.5", "2.0"),
            BoilerSlot.create(1, "06:00:00", "3.6", "2.0"),
        ]
    )
    assert serialize_boiler_schedule(schedule) == (
        b'[{"DayOfWeek":1,"Hour":"06:00:00","Target":3.6,"Min":2.0},'
        b'{"DayOfWeek":1,"Hour":"18:00:00","Target":3.5,"Min":2.0},'
        b'{"DayOfWeek":2,"Hour":"06:00:00","Target":4,"Min":2}]'
    )


def test_numbers_are_written_as_they_were_entered() -> None:
    schedule = BoilerSchedule.of([BoilerSlot.create(1, "06:00:00", "3", "3.0")])
    assert serialize_boiler_schedule(schedule).endswith(b'"Target":3,"Min":3.0}]')


# --- the empty file rules (OQ-17 evidence) ----------------------------------------------------


def test_an_empty_schedule_is_never_written() -> None:
    with pytest.raises(ScheduleFileError, match="empty boiler schedule"):
        serialize_boiler_schedule(BoilerSchedule())
    with pytest.raises(ScheduleFileError, match="empty timer schedule"):
        serialize_timer_schedule(TimerSchedule())
    with pytest.raises(ScheduleFileError, match="at least one entry"):
        serialize_relay_manual([])


def test_auto_is_the_empty_manual_file_and_reads_back_as_auto() -> None:
    assert serialize_manual(ManualOverride()) == b"[]"
    assert parse_manual(b"[]").is_auto


def test_the_legacy_reader_turns_an_empty_list_into_one_default_task() -> None:
    """The hazard behind OQ-17: the services' parser sees a phantom entry (day 0) in ``[]``."""
    assert legacy_reader.parse("[]") == [{"day": 0, "hour": "00:00", "a": "0", "b": "0"}]


# --- the reader is tolerant about layout and strict about content -----------------------------


def test_reader_accepts_white_space_bom_and_unsorted_duplicated_input() -> None:
    text = json.dumps(
        [
            {"DayOfWeek": 2, "Hour": "06:00:00", "Target": 4.0, "Min": 2.0},
            {"DayOfWeek": 1, "Hour": "06:00:00", "Target": 3.6, "Min": 2.0},
            {"DayOfWeek": 1, "Hour": "06:00:00", "Target": 3.6, "Min": 2.0},
        ],
        indent=2,
    )
    schedule = parse_boiler_schedule(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert [(s.day, s.target) for s in schedule.slots] == [(1, Decimal("3.6")), (2, Decimal("4.0"))]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "not valid JSON"),
        (b"[{", "not valid JSON"),
        (b"\xff\xfe", "not valid JSON"),
        (b"{}", "list of objects"),
        (b"[1]", "list of objects"),
        (b"[]", "boiler schedule is empty"),
        (b'[{"DayOfWeek":1,"Hour":"06:00:00","Target":3}]', "expected exactly the keys"),
        (
            b'[{"DayOfWeek":1,"Hour":"06:00:00","Target":3,"Min":2,"Extra":1}]',
            "expected exactly the keys",
        ),
        (b'[{"DayOfWeek":1,"Hour":"06:00:00","Target":"3","Min":2}]', "Target must be a number"),
        (b'[{"DayOfWeek":true,"Hour":"06:00:00","Target":3,"Min":2}]', "DayOfWeek must be a whole"),
        (b'[{"DayOfWeek":1.0,"Hour":"06:00:00","Target":3,"Min":2}]', "DayOfWeek must be a whole"),
        (b'[{"DayOfWeek":0,"Hour":"06:00:00","Target":3,"Min":2}]', "day of week"),
        (b'[{"DayOfWeek":1,"Hour":"6:00:00","Target":3,"Min":2}]', "Hour must look like"),
        (b'[{"DayOfWeek":1,"Hour":"06:00","Target":3,"Min":2}]', "Hour must look like"),
        (b'[{"DayOfWeek":1,"Hour":"25:00:00","Target":3,"Min":2}]', "not a valid time"),
        (b'[{"DayOfWeek":1,"Hour":"06:00:00","Target":NaN,"Min":2}]', "NaN is not allowed"),
    ],
)
def test_reader_rejects_bad_boiler_files(data: bytes, message: str) -> None:
    with pytest.raises(ScheduleFileError, match=message):
        parse_boiler_schedule(data)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"[]", "timer schedule is empty"),
        (b'[{"DayOfWeek":1,"Hour":"06:00:00","State":2,"RelayNum":4}]', "state must be 0 or 1"),
        (b'[{"DayOfWeek":1,"Hour":"06:00:00","State":1,"RelayNum":9}]', "relay must be"),
        (b'[{"DayOfWeek":1,"Hour":"06:00:00","State":1}]', "expected exactly the keys"),
        (b'[{"DayOfWeek":1,"Hour":"06:00:00","State":1.5,"RelayNum":4}]', "State must be a whole"),
    ],
)
def test_reader_rejects_bad_timer_files(data: bytes, message: str) -> None:
    with pytest.raises(ScheduleFileError, match=message):
        parse_timer_schedule(data)


def test_manual_file_with_two_entries_is_rejected() -> None:
    entry = '{"DayOfWeek":7,"Hour":"19:00:00","Target":4,"Min":4}'
    with pytest.raises(ScheduleFileError, match="at most one entry"):
        parse_manual(f"[{entry},{entry}]")


def test_reader_does_not_hide_a_model_error_behind_a_traceback() -> None:
    bad: dict[str, Any] = {"DayOfWeek": 9, "Hour": "06:00:00", "Target": 3, "Min": 2}
    with pytest.raises(ScheduleFileError, match="day of week"):
        parse_manual(json.dumps([bad]))
