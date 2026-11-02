"""Calendar events -> boiler and timer schedules (FR-CFG-1, FR-CFG-6, FR-CFG-8, section 3.9)."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orchestrator.astro import Location, resolve_astro
from orchestrator.calendar_schedules import (
    CalendarSchedules,
    build_schedules,
    read_schedules,
)
from orchestrator.calendar_source import CalendarAuthError, CalendarEvent, GoogleCalendarSource
from orchestrator.event_titles import AstroTime
from orchestrator.models import BoilerSlot, TimerEntry, format_number
from orchestrator.relays import RelayNames
from orchestrator.schedule_files import (
    parse_boiler_schedule,
    parse_timer_schedule,
    serialize_boiler_schedule,
)
from orchestrator.validation import check_boiler_schedule
from orchestrator.weekdays import boiler_day, day_and_time
from tests.fake_google import FakeCalendarService, FakeHttpError, google_event
from tests.fakes import FakeClock

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Jerusalem")
LOCATION = Location(32.07, 34.82)  # approximately Ramat Gan
SUNDAY = date(2026, 10, 18)  # day 1 of the week of the real sample files


def at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=TZ)


def event(
    summary: str, start: datetime, end: datetime | None = None, *, all_day: bool = False
) -> CalendarEvent:
    return CalendarEvent(f"id-{summary}-{start:%Y%m%d%H%M}", summary, start, end, all_day)


def build(*events: CalendarEvent, location: Location | None = LOCATION) -> CalendarSchedules:
    return build_schedules(events, tz=TZ, location=location)


def entries(result: CalendarSchedules) -> list[tuple[int, time, int, int]]:
    return [(e.day, e.at, e.state, e.relay) for e in result.timer.entries]


# --- the real sample files, rebuilt from calendar events --------------------------------------


def test_cal_json_is_rebuilt_byte_for_byte_from_boiler_events() -> None:
    original = (ROOT / "Cal.json").read_bytes()
    events = [
        event(
            f"Boiler: target={format_number(slot.target)}; min={format_number(slot.minimum)}",
            datetime.combine(SUNDAY + timedelta(days=slot.day - 1), slot.start, tzinfo=TZ),
        )
        for slot in parse_boiler_schedule(original).slots
    ]
    result = build(*events)
    assert result.problems == ()
    assert serialize_boiler_schedule(result.boiler) == original
    assert result.commands_found == len(events) == result.events_seen


def test_the_real_time_cal_json_is_rebuilt_from_timer_events() -> None:
    expected = parse_timer_schedule((ROOT / "TimeCal.json").read_bytes())
    events = [
        event("Timer: relaynum 4 start sunset -30 end 23:00", at(SUNDAY + timedelta(days=d), 17))
        for d in range(7)
    ]
    result = build(*events)
    assert result.problems == ()
    assert len(result.timer.entries) == len(expected.entries) == 14
    for built, legacy in zip(result.timer.entries, expected.entries, strict=True):
        assert (built.day, built.state, built.relay) == (legacy.day, legacy.state, legacy.relay)
        built_s = built.at.hour * 3600 + built.at.minute * 60 + built.at.second
        legacy_s = legacy.at.hour * 3600 + legacy.at.minute * 60 + legacy.at.second
        assert abs(built_s - legacy_s) <= 2


# --- boiler events ----------------------------------------------------------------------------


def test_a_boiler_event_gives_a_slot_at_its_own_day_and_start_time() -> None:
    result = build(event("Boiler: target=3.6; min=2", at(date(2026, 10, 20), 6, 0, 30)))
    assert result.boiler.slots == (
        BoilerSlot(3, time(6, 0, 30), Decimal("3.6"), Decimal("2")),  # Tuesday = day 3
    )


def test_a_value_above_the_limit_is_rejected_and_reported_never_clamped() -> None:
    result = build(
        event("Boiler: target=6; min=2", at(date(2026, 10, 20), 6)),
        event("Boiler: target=5; min=5", at(date(2026, 10, 20), 7)),
    )
    assert [s.start for s in result.boiler.slots] == [time(7, 0)]  # target 5 is fine, 6 is not
    assert len(result.problems) == 1
    assert "Tuesday 2026-10-20 06:00 'Boiler: target=6; min=2'" in result.problems[0]
    assert "above the limit of 5" in result.problems[0]


def test_min_above_target_is_accepted_with_a_warning() -> None:
    result = build(event("Boiler: target=3.0; min=3.5", at(date(2026, 10, 20), 13)))
    assert len(result.boiler.slots) == 1 and result.problems == ()
    assert "Min 3.5 is above Target 3.0" in result.warnings[0]


def test_identical_events_make_one_slot_but_different_ones_at_the_same_time_are_both_kept() -> None:
    twin = at(date(2026, 10, 20), 6)
    same = build(event("Boiler: target=3; min=2", twin), event("Boiler: target=3; min=2", twin))
    assert len(same.boiler.slots) == 1
    clash = build(event("Boiler: target=3; min=2", twin), event("Boiler: target=4; min=2", twin))
    assert len(clash.boiler.slots) == 2
    assert not check_boiler_schedule(clash.boiler).ok  # the validation rejects the clash later


# --- timer events -----------------------------------------------------------------------------


def test_a_timer_without_astro_runs_from_the_event_start_to_the_event_end() -> None:
    day = date(2026, 10, 20)
    result = build(event("Timer: relaynum 4", at(day, 17, 30), at(day, 23)))
    assert entries(result) == [(3, time(17, 30), 1, 4), (3, time(23, 0), 0, 4)]


def test_the_title_end_duration_and_clock_start_are_ignored_without_astro() -> None:
    day = date(2026, 10, 20)
    title = "Timer: relaynum GardenLights start 18:00 end 20:00 duration 60"
    result = build(event(title, at(day, 17, 30), at(day, 23)))
    assert entries(result) == [(3, time(17, 30), 1, 4), (3, time(23, 0), 0, 4)]


def test_a_timer_without_astro_can_run_past_midnight_from_saturday_into_sunday() -> None:
    result = build(
        event("Timer: relaynum 4", at(date(2026, 10, 24), 23, 30), at(date(2026, 10, 25), 0, 30))
    )
    assert entries(result) == [(1, time(0, 30), 0, 4), (7, time(23, 30), 1, 4)]  # Sunday first


def test_a_timer_ending_exactly_at_midnight_ends_on_the_next_day_at_zero() -> None:
    result = build(
        event("Timer: relaynum 4", at(date(2026, 10, 24), 22), at(date(2026, 10, 25), 0))
    )
    assert entries(result) == [(1, time(0, 0), 0, 4), (7, time(22, 0), 1, 4)]


def test_a_timer_without_astro_needs_an_event_end_after_its_start() -> None:
    day = date(2026, 10, 20)
    no_end = build(event("Timer: relaynum 4 duration 60", at(day, 17)))
    same = build(event("Timer: relaynum 4", at(day, 17), at(day, 17)))
    for result in (no_end, same):
        assert result.timer.entries == ()
        assert "needs an event end after its start" in result.problems[0]


def test_an_astro_end_before_the_start_means_the_next_day() -> None:
    tuesday = build(event("Timer: relaynum 4 start sunset end 06:00", at(date(2026, 10, 20), 12)))
    assert [(e.day, e.at, e.state) for e in tuesday.timer.entries][1:] == [(4, time(6, 0), 0)]
    saturday = build(event("Timer: relaynum 4 start sunset end 06:00", at(date(2026, 10, 24), 12)))
    assert (1, time(6, 0), 0, 4) in entries(saturday)  # Saturday night ends on Sunday morning


def test_with_astro_a_duration_gives_the_off_time_and_an_end_time_wins_over_it() -> None:
    day = date(2026, 10, 20)
    by_duration = build(event("Timer: relaynum 4 start dusk duration 3600", at(day, 12)))
    on, off = by_duration.timer.entries
    assert (off.day, off.state) == (3, 0) and on.at < off.at
    by_end = build(event("Timer: relaynum 4 start dusk end 23:00 duration 60", at(day, 12)))
    assert (3, time(23, 0), 0, 4) in entries(by_end)


def test_every_relay_gets_its_own_on_and_off_entry() -> None:
    day = date(2026, 10, 20)
    result = build(event("Timer: relaynum 4 GardenRoof", at(day, 17), at(day, 18)))
    pairs = sorted((e.relay, e.state) for e in result.timer.entries)
    assert pairs == [(2, 0), (2, 1), (4, 0), (4, 1)]  # each relay once on and once off


def test_an_astro_end_before_the_start_is_resolved_on_the_next_date() -> None:
    day = date(2026, 10, 20)
    result = build(event("Timer: relaynum 4 start sunset end sunrise", at(day, 12)))
    on_day, on_at = day_and_time(resolve_astro(AstroTime("sunset"), day, LOCATION, TZ))
    next_day = day + timedelta(days=1)
    off_day, off_at = day_and_time(resolve_astro(AstroTime("sunrise"), next_day, LOCATION, TZ))
    assert (on_day, off_day) == (boiler_day(day), boiler_day(next_day))
    assert entries(result) == [(on_day, on_at, 1, 4), (off_day, off_at, 0, 4)]


def test_an_astro_start_that_falls_on_the_previous_date_uses_that_date_for_the_day() -> None:
    # midnight - 30 minutes of Tuesday is Monday 23:5x; the legacy code kept Tuesday as the day
    result = build(
        event("Timer: relaynum 4 start midnight -30 duration 600", at(date(2026, 10, 20), 12))
    )
    on, off = sorted(result.timer.entries, key=lambda e: e.state, reverse=True)
    assert (on.state, on.day) == (1, 2)  # Monday
    assert on.at.hour == 23 and on.at.minute >= 50
    assert (off.state, off.day) == (0, 3)  # ten minutes later it is Tuesday


# --- things that cannot be used: skipped and reported, the rest still works -------------------


def test_a_sun_time_without_a_location_is_reported_and_the_other_events_still_work() -> None:
    result = build(
        event("Timer: relaynum 4 start sunset end 23:00", at(date(2026, 10, 20), 12)),
        event("Boiler: target=3; min=2", at(date(2026, 10, 20), 6)),
        location=None,
    )
    assert len(result.problems) == 1 and "no location is configured" in result.problems[0]
    assert result.timer.entries == ()
    assert len(result.boiler.slots) == 1


def test_bad_titles_are_reported_with_their_reason_and_ordinary_events_are_ignored() -> None:
    result = build(
        event("Boiler: target=3", at(date(2026, 10, 20), 6)),
        event("Timer: relaynum nobody duration 60", at(date(2026, 10, 20), 7)),
        event("Dentist 10:00", at(date(2026, 10, 20), 10)),
        event("Boiler: target=3; min=2", at(date(2026, 10, 20), 8)),
    )
    assert len(result.problems) == 2
    assert "min is missing" in result.problems[0]
    assert "no relay found" in result.problems[1]
    assert (result.events_seen, result.commands_found) == (4, 1)


def test_an_all_day_command_is_reported() -> None:
    result = build(event("Timer: relaynum 4 duration 60", at(date(2026, 10, 20), 0), all_day=True))
    label = "Tuesday 2026-10-20 00:00 'Timer: relaynum 4 duration 60'"
    assert result.problems == (f"{label}: an all-day event has no start time",)
    assert result.timer.entries == ()


def test_long_titles_are_shortened_in_messages() -> None:
    result = build(event("Boiler: target=3 " + "x" * 200, at(date(2026, 10, 20), 6)))
    assert len(result.problems[0]) < 200 and "..." in result.problems[0]


# --- relay names ------------------------------------------------------------------------------


def test_an_unknown_relay_name_is_a_warning_and_the_event_still_works() -> None:
    result = build(
        event("Timer: relaynum 4 garden", at(date(2026, 10, 20), 17), at(date(2026, 10, 20), 18))
    )
    assert result.problems == () and len(result.timer.entries) == 2
    assert "unknown relay name 'garden' ignored" in result.warnings[0]


def test_an_ambiguous_relay_name_warns_and_uses_the_first_listing() -> None:
    result = build(
        event(
            "Timer: relaynum spareGround2", at(date(2026, 10, 20), 17), at(date(2026, 10, 20), 18)
        )
    )
    assert {e.relay for e in result.timer.entries} == {6}
    assert "listed for several relays; using 6" in result.warnings[0]


def test_custom_relay_names_from_the_config_are_used() -> None:
    names = RelayNames([("Fountain", 3)])
    result = build_schedules(
        [event("Timer: relaynum fountain", at(date(2026, 10, 20), 17), at(date(2026, 10, 20), 18))],
        tz=TZ,
        location=LOCATION,
        relay_names=names,
    )
    assert {e.relay for e in result.timer.entries} == {3}


# --- reading the current week through the source ----------------------------------------------


NOW = datetime(2026, 10, 24, 16, 41, 0, tzinfo=TZ)  # a Saturday


def test_the_current_week_is_read_and_turned_into_schedules() -> None:
    service = FakeCalendarService(
        [
            {
                "items": [
                    google_event("Boiler: target=3.6; min=2", "2026-10-20T06:00:00+03:00", id="1"),
                    google_event(
                        "Timer: relaynum 4",
                        "2026-10-27T17:30:00+02:00",
                        "2026-10-27T23:00:00+02:00",
                        id="2",
                    ),
                    google_event("Dentist", "2026-10-21T10:00:00+03:00", id="3"),
                    google_event("Boiler: target=3", "2026-10-22T06:00:00+03:00", id="4"),
                    google_event("broken", "not-a-time", id="5"),
                ]
            }
        ]
    )
    clock = FakeClock(NOW)
    result = read_schedules(GoogleCalendarSource(service, "primary", TZ), clock, location=LOCATION)
    assert service.calls[0]["timeMin"] == "2026-10-24T00:00:00+03:00"
    assert service.calls[0]["timeMax"] == "2026-10-31T00:00:00+02:00"
    assert len(result.boiler.slots) == 1 and len(result.timer.entries) == 2
    assert any("min is missing" in p for p in result.problems)
    assert any(w == "calendar: event 5: unreadable start or end time" for w in result.warnings)
    assert (result.events_seen, result.commands_found) == (4, 2)


def test_a_calendar_error_is_not_swallowed() -> None:
    service = FakeCalendarService(error=FakeHttpError(401))
    with pytest.raises(CalendarAuthError):
        read_schedules(
            GoogleCalendarSource(service, "primary", TZ), FakeClock(NOW), location=LOCATION
        )


def test_timer_entries_are_plain_model_objects() -> None:
    day = date(2026, 10, 20)
    result = build(event("Timer: relaynum 4", at(day, 17), at(day, 18)))
    assert all(isinstance(e, TimerEntry) for e in result.timer.entries)
