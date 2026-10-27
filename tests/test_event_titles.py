"""The calendar event-title grammar and the relay names (FR-CFG-6, Requirements 3.7 and 3.9)."""

import random
from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from orchestrator.config import ConfigError, load_config
from orchestrator.event_titles import (
    ASTRO_NAMES,
    AstroTime,
    BoilerCommand,
    TimerCommand,
    TitleResult,
    UnrepresentableError,
    format_boiler_title,
    format_timer_title,
    parse_title,
)
from orchestrator.models import ModelError, format_number
from orchestrator.relays import DEFAULT_RELAY_NAME_PAIRS, RelayNames

NAMES = RelayNames()


def boiler(title: str) -> BoilerCommand:
    result = parse_title(title, NAMES)
    assert isinstance(result.command, BoilerCommand), result
    return result.command


def timer(title: str) -> TimerCommand:
    result = parse_title(title, NAMES)
    assert isinstance(result.command, TimerCommand), result
    return result.command


def problem(title: str) -> str:
    result = parse_title(title, NAMES)
    assert result.command is None and result.problem, result
    return result.problem


# --- boiler titles ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Boiler: target=3.5; min=2",
        "Boiler: Target 3.5 Min 2",
        "boiler:target=3.5,min=2",
        "BOILER: TARGET=3.5 MIN=2",
        "Boiler: min=2; target=3.5",
        "Boiler: morning shower target=3.5 min=2 enjoy",
        "Boiler:   target = 3.5 ;   min = 2  ",
        "My Boiler: target 3.5 min 2",
    ],
)
def test_boiler_titles_in_every_spelling(title: str) -> None:
    command = boiler(title)
    assert (format_number(command.target), format_number(command.minimum)) == ("3.5", "2")


def test_numbers_keep_the_way_they_were_written() -> None:
    command = boiler("Boiler: target=4 min=3.0")
    assert (format_number(command.target), format_number(command.minimum)) == ("4", "3.0")


def test_a_decimal_comma_is_read_like_the_legacy_parser_did() -> None:
    # the comma is a separator, so "3,5" is target 3 followed by an unrelated 5 (legacy behaviour)
    command = boiler("Boiler: target=3,5 min=2")
    assert command.target == Decimal("3")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Boiler: target=3.5", "min is missing"),
        ("Boiler: min=2", "target is missing"),
        ("Boiler:", "target is missing; min is missing"),
        ("Boiler: target=abc min=2", "target must be a number"),
        ("Boiler: target=² min=2", "target must be a number"),  # found by the fuzz test
        ("Boiler: target=3 min=٣", "min must be a number"),
        ("Boiler: target=3 min=-1", "min must be a number"),
        ("Boiler: target", "target has no value; min is missing"),
        ("Boiler: min=2 target", "target has no value"),
    ],
)
def test_wrong_boiler_titles_give_a_reason_instead_of_crashing(title: str, expected: str) -> None:
    assert expected in problem(title)


@pytest.mark.parametrize(
    "title",
    [
        "Boiler: boiler target=3 min=2",  # legacy: NameError on the bare word
        "Boiler: target=3 boiler min=2",
        "Boiler: min=2 target=",  # legacy: IndexError
        "Boiler: target=3 min",  # legacy: IndexError
    ],
)
def test_titles_that_crashed_the_legacy_parser_never_raise(title: str) -> None:
    result = parse_title(title, NAMES)
    assert result.command is not None or result.problem


def test_a_huge_number_is_accepted_as_text_without_crashing() -> None:
    # beyond Python's integer-conversion limit; the 5-shower validation rejects it later
    command = boiler("Boiler: target=" + "9" * 5000 + " min=2")
    assert command.target > Decimal("5") and command.minimum == Decimal("2")


def test_the_limit_of_5_is_not_checked_by_the_parser() -> None:
    assert boiler("Boiler: target=6 min=2").target == Decimal("6")  # validation rejects it later


# --- timer titles -----------------------------------------------------------------------------


def test_the_example_from_the_requirements() -> None:
    command = timer("Timer: relaynum 4 start sunset +30 end 23:00")
    assert command == TimerCommand(
        relays=(4,),
        start=AstroTime("sunset", 30),
        end_clock=time(23, 0, 0),
        end_astro=None,
        duration_s=None,
    )


def test_a_relay_name_and_a_duration() -> None:
    command = timer("Timer: relaynum GardenLights duration 3600")
    assert command.relays == (4,)
    assert command.duration_s == 3600
    assert command.start is None and command.end_clock is None


def test_several_relays_by_number_and_name_in_any_case() -> None:
    command = timer("Timer: relaynum 2 gardenroof SPAREROOF 2 duration 60")
    assert command.relays == (2, 3)  # the duplicate 2 is dropped, order kept


def test_start_and_end_both_astro_with_keywords() -> None:
    command = timer("Timer: relaynum 4 start sunset end sunrise")
    assert command.start == AstroTime("sunset") and command.end_astro == AstroTime("sunrise")
    assert command.end_clock is None


def test_two_astro_words_without_keywords_are_start_then_end() -> None:
    command = timer("Timer: relaynum 4 sunset sunrise")
    assert command.start == AstroTime("sunset") and command.end_astro == AstroTime("sunrise")


def test_an_astro_word_after_end_is_the_end_even_without_a_start_word() -> None:
    command = timer("Timer: relaynum 4 sunset end dawn")
    assert command.start == AstroTime("sunset") and command.end_astro == AstroTime("dawn")


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("sunset +30", 30),
        ("sunset + 30", 30),
        ("sunset -15", -15),
        ("sunset - 15", -15),
        ("sunset", 0),
        ("sunset +1", 1),
    ],  # a single digit is minutes, as the legacy code behaved
)
def test_astro_offsets_are_whole_minutes(text: str, minutes: int) -> None:
    command = timer(f"Timer: relaynum 4 start {text} duration 60")
    assert command.start == AstroTime("sunset", minutes)


@pytest.mark.parametrize(
    ("end", "expected"),
    [
        ("23", time(23, 0, 0)),
        ("23:30", time(23, 30, 0)),
        ("6 30 15", time(6, 30, 15)),
        ("0:00", time(0, 0)),
    ],
)
def test_end_clock_times(end: str, expected: time) -> None:
    assert timer(f"Timer: relaynum 4 end {end}").end_clock == expected


@pytest.mark.parametrize("name", ["twilight", "midnight", "moonrise", "moonset", "dusk", "noon"])
def test_every_astro_name_is_accepted(name: str) -> None:
    assert timer(f"Timer: relaynum 4 start {name} duration 60").start == AstroTime(name)


def test_end_and_duration_can_both_be_given_and_both_are_kept() -> None:
    command = timer("Timer: relaynum 4 end 23:00 duration 3600")
    assert command.end_clock == time(23, 0) and command.duration_s == 3600  # the end wins later


def test_the_boiler_form_wins_if_a_title_contains_both() -> None:
    assert isinstance(
        parse_title("Boiler: target=3 min=2 Timer: relaynum 4 duration 5", NAMES).command,
        BoilerCommand,
    )


def test_an_ambiguous_relay_name_warns_and_uses_the_first_listing() -> None:
    result = parse_title("Timer: relaynum spareGround2 duration 60", NAMES)
    assert isinstance(result.command, TimerCommand) and result.command.relays == (6,)
    assert result.warnings == ("relay name 'spareground2' is listed for several relays; using 6",)


def test_an_unknown_relay_name_is_ignored_with_a_warning() -> None:
    result = parse_title("Timer: relaynum 4 garden duration 60", NAMES)
    assert isinstance(result.command, TimerCommand) and result.command.relays == (4,)
    assert result.warnings == ("unknown relay name 'garden' ignored",)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Timer: duration 60", "no relay found"),
        ("Timer: relaynum", "no relay found"),
        ("Timer: relaynum 4", "needs an end time or a duration"),
        ("Timer: relaynum nobody duration 60", "no relay found"),
        ("Timer: relaynum 9 duration 60", "relay 9 does not exist"),
        ("Timer: relaynum 4 duration abc", "duration must be a number of seconds"),
        ("Timer: relaynum 4 duration 0", "duration must be a number of seconds"),
        ("Timer: relaynum 4 duration", "duration must be a number of seconds"),
        ("Timer: relaynum 4 start sunset + end 23", "whole minutes"),
        ("Timer: relaynum 4 start sunset +x end 23", "whole minutes"),
        ("Timer: relaynum 4 end 25", "not a valid time"),
        ("Timer: relaynum 4 end 23 61", "not a valid time"),
        ("Timer: relaynum 4 end 1 2 3 4", "too many numbers"),
        (
            "Timer: relaynum 4 end 99999999999999999999",
            "not a valid time",
        ),  # found by the fuzz test
        ("Timer: relaynum 4 duration ²", "duration must be a number"),  # found by the fuzz test
        ("Timer: relaynum 4 duration ٣", "duration must be a number"),  # non-ASCII digits
        ("Timer: relaynum 4 start sunset + ² end 23", "whole minutes"),
        ("Timer: relaynum 4 end ²", "needs an end time or a duration"),
        ("Timer: relaynum ² duration 60", "no relay found"),
        ("Timer: relaynum 4 duration " + "9" * 5000, "1 to 86400"),  # beyond Python's int limit
        ("Timer: relaynum " + "9" * 5000 + " duration 60", "does not exist"),
        ("Timer: relaynum 4 end " + "9" * 5000, "not a valid time"),
        ("Timer: relaynum 4 start sunset +" + "9" * 5000 + " end 23", "at most 1440 minutes"),
        ("Timer: relaynum 4 duration 86401", "1 to 86400"),
        ("Timer: relaynum 4 duration 99999999999999999999", "1 to 86400"),
        ("Timer: relaynum 4 start sunset +1441 end 23", "at most 1440 minutes"),
        ("Timer: relaynum 4 start sunset -99999999999999999999 end 23", "at most 1440 minutes"),
    ],
)
def test_wrong_timer_titles_give_a_reason(title: str, expected: str) -> None:
    assert expected in problem(title)


@pytest.mark.parametrize(
    "title",
    [
        "Timer: timer relaynum 4 duration 60",  # legacy: NameError on the bare word
        "Timer: relaynum 4 start",
        "Timer: relaynum 4 end",
        "Timer: relaynum 4 duration",
        "Timer: relaynum + - 4 duration 60",
    ],
)
def test_timer_titles_that_crashed_the_legacy_parser_never_raise(title: str) -> None:
    result = parse_title(title, NAMES)
    assert result.command is not None or result.problem


def test_random_titles_never_raise_and_always_give_a_consistent_result() -> None:
    rng = random.Random(20261024)
    vocabulary = [
        "boiler:", "timer:", "Boiler:", "Timer:", "target", "min", "=", ";", ",", ":", "+", "-",
        "relaynum", "start", "end", "duration", "sunset", "sunrise", "twilight", "midnight",
        "0", "1", "4", "7", "9", "23", "30", "61", "3.5", "2.", ".5", "-1", "abc", "GardenLights",
        "nobody", "", " ", "שם", "1e9", "99999999999999999999",
        "²", "٣", "ﬁ", "\x00", "\n",  # superscript two, Arabic-Indic three, ligature
        "dawn", "dusk", "noon", "moonrise", "moonset",
    ]  # fmt: skip
    for _ in range(5000):
        title = " ".join(rng.choice(vocabulary) for _ in range(rng.randint(0, 12)))
        result = parse_title(title, NAMES)
        if result.command is not None:
            assert result.problem is None, title
        if isinstance(result.command, TimerCommand):
            assert result.command.relays, title
            assert (
                result.command.end_clock or result.command.end_astro or result.command.duration_s
            ), title
        if isinstance(result.command, BoilerCommand):
            assert result.command.target >= 0 and result.command.minimum >= 0, title


# --- ordinary events --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title", ["", "   ", "Dentist 10:00", "Birthday party", "boiler service", "timer"]
)
def test_ordinary_events_are_not_commands_and_not_problems(title: str) -> None:
    assert parse_title(title, NAMES) == TitleResult()


# --- relay names ------------------------------------------------------------------------------


def test_default_names_match_the_legacy_list() -> None:
    assert NAMES.lookup("GardenLights") == 4
    assert NAMES.lookup("  gardenlights ") == 4
    assert NAMES.lookup("BolerLight") == 1  # the legacy spelling
    assert NAMES.lookup("BoilerLight") is None
    assert NAMES.lookup("nothing") is None
    assert NAMES.name_of(4) == "GardenLights" and NAMES.name_of(7) == "spareGround2"


def test_a_name_listed_for_two_relays_is_reported_and_the_first_wins() -> None:
    assert NAMES.duplicates == ("spareGround2",)
    assert NAMES.lookup("spareground2") == 6
    assert len(DEFAULT_RELAY_NAME_PAIRS) == 8


@pytest.mark.parametrize("bad_name", ["", "two words", "a-b", "a+b", "a:b", "a=b", "a;b", "a,b"])
def test_relay_names_must_be_a_single_plain_word(bad_name: str) -> None:
    with pytest.raises(ModelError, match="one word"):
        RelayNames([(bad_name, 2)])


@pytest.mark.parametrize("relay", [-1, 8, True])
def test_relay_numbers_must_exist(relay: int) -> None:
    with pytest.raises(ModelError, match="relay number"):
        RelayNames([("Lamp", relay)])


def test_custom_names_are_used_by_the_parser() -> None:
    names = RelayNames([("Lamp", 3), ("Fountain", 4)])
    result = parse_title("Timer: relaynum lamp fountain duration 60", names)
    assert isinstance(result.command, TimerCommand) and result.command.relays == (3, 4)
    assert names.duplicates == ()


# --- configuration of the names ---------------------------------------------------------------


def test_the_config_has_the_legacy_names_by_default() -> None:
    assert load_config(None, env={}).relays.names == DEFAULT_RELAY_NAME_PAIRS


def test_the_config_can_rename_relays(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[relays]\nnames = [["Lamp", 3], ["Fountain", 4]]\n', encoding="utf-8")
    config = load_config(path, env={})
    assert config.relays.names == (("Lamp", 3), ("Fountain", 4))
    assert RelayNames(config.relays.names).lookup("fountain") == 4


@pytest.mark.parametrize(
    "text",
    [
        'names = "Lamp"',
        'names = [["Lamp"]]',
        'names = [["Lamp", "3"]]',
        'names = [["Lamp", 9]]',
        'names = [["two words", 3]]',
        "names = [[3, 3]]",
    ],
)
def test_bad_relay_names_in_the_config_are_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[relays]\n{text}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[relays\] names"):
        load_config(path, env={})


# --- writing titles ---------------------------------------------------------------------------


def test_a_boiler_title_is_written_with_the_numbers_as_entered() -> None:
    command = BoilerCommand(Decimal("3.50"), Decimal("2"))
    assert format_boiler_title(command) == "Boiler: target=3.50; min=2"
    assert parse_title(format_boiler_title(command), NAMES).command == command


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            TimerCommand((4, 5), AstroTime("sunset", -30), time(23, 0), None, None),
            "Timer: relaynum GardenLights intercom start sunset -30 end 23:00:00",
        ),
        (
            TimerCommand((7,), None, None, None, 600),
            "Timer: relaynum 7 duration 600",
        ),  # ambiguous name
        (
            TimerCommand((2,), AstroTime("sunset"), None, AstroTime("sunrise", 10), None),
            "Timer: relaynum GardenRoof start sunset end sunrise +10",
        ),
        (
            TimerCommand((4,), None, time(6, 5, 9), None, None),
            "Timer: relaynum GardenLights end 06:05:09",
        ),
        (
            TimerCommand((4,), AstroTime("noon", 5), None, None, 60),
            "Timer: relaynum GardenLights start noon +5 duration 60",
        ),
    ],
)
def test_timer_titles_are_written_in_the_documented_grammar(
    command: TimerCommand, expected: str
) -> None:
    assert format_timer_title(command, NAMES) == expected
    assert parse_title(expected, NAMES).command == command


@pytest.mark.parametrize(
    "command",
    [
        TimerCommand(
            (4,), None, None, AstroTime("sunrise"), None
        ),  # the first astro word is a start
        TimerCommand((4,), AstroTime("sunset"), time(23, 0), AstroTime("sunrise"), None),
        TimerCommand((4,), None, None, None, None),  # no end and no duration
        TimerCommand((), None, None, None, 60),  # no relay
        TimerCommand((4,), None, None, None, 86_401),  # longer than the parser accepts
        TimerCommand((4,), AstroTime("sunset", 2000), None, None, 60),  # offset beyond a day
    ],
)
def test_commands_that_would_not_read_back_are_refused(command: TimerCommand) -> None:
    with pytest.raises(UnrepresentableError):
        format_timer_title(command, NAMES)


def test_a_negative_boiler_value_cannot_be_written() -> None:
    with pytest.raises(UnrepresentableError):
        format_boiler_title(BoilerCommand(Decimal("-1"), Decimal("2")))


def test_random_commands_either_round_trip_exactly_or_are_refused_for_a_known_reason() -> None:
    rng = random.Random(20261025)
    astro = sorted(ASTRO_NAMES)
    for _ in range(4000):
        relays = tuple(rng.sample(range(8), rng.randint(1, 4)))
        start = (
            AstroTime(rng.choice(astro), rng.randint(-1440, 1440)) if rng.random() < 0.5 else None
        )
        kind = rng.choice(["none", "clock", "astro"])
        end_clock = (
            time(rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
            if kind == "clock"
            else None
        )
        end_astro = (
            AstroTime(rng.choice(astro), rng.randint(-1440, 1440)) if kind == "astro" else None
        )
        duration = rng.randint(1, 86_400) if rng.random() < 0.5 else None
        command = TimerCommand(relays, start, end_clock, end_astro, duration)
        impossible = (end_astro is not None and start is None) or (
            kind == "none" and duration is None
        )
        if impossible:
            with pytest.raises(UnrepresentableError):
                format_timer_title(command, NAMES)
        else:
            title = format_timer_title(command, NAMES)
            assert parse_title(title, NAMES).command == command, title
