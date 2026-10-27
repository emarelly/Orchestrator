"""The fake boiler Pi itself, exercised through the real BoilerClient (no sockets are opened)."""

import json
import time
from collections.abc import Iterator
from datetime import time as clock_time
from decimal import Decimal

import pytest

from orchestrator.boiler_client import (
    BoilerBadAnswer,
    BoilerClient,
    BoilerRejected,
    BoilerUnreachable,
)
from orchestrator.manual import ManualUpload, build_auto, manual_upload, relay_filename
from orchestrator.models import (
    BoilerSchedule,
    BoilerSlot,
    ManualEntry,
    ManualOverride,
    TimerEntry,
    TimerSchedule,
)
from orchestrator.schedule_files import (
    serialize_boiler_schedule,
    serialize_relay_manual,
    serialize_timer_schedule,
)
from tests.fake_boiler import FakeBoilerPi


@pytest.fixture
def pi() -> FakeBoilerPi:
    return FakeBoilerPi()


@pytest.fixture
def client(pi: FakeBoilerPi) -> Iterator[BoilerClient]:
    with pi.session() as session:
        yield BoilerClient(pi.config, session=session)


SCHEDULE = BoilerSchedule.of([BoilerSlot.create(1, "06:00:00", "3.6", "2.0")])
TIMER = TimerSchedule.of(
    [TimerEntry(1, clock_time(17, 30), 1, 4), TimerEntry(1, clock_time(23), 0, 4)]
)


# --- reading ----------------------------------------------------------------------------------


def test_status_history_log_and_relay_endpoints_answer(
    pi: FakeBoilerPi, client: BoilerClient
) -> None:
    pi.history_rows = [[2026, 10, 24, 16, 40, 19, 3.43, 2.0, 3.0, 0, 4.92]]
    pi.log_lines = ["a", "b"]
    assert client.get_status().mode == "Auto"
    assert len(client.get_history()) == 1
    assert client.get_log() == "a\nb"
    assert client.get_relay_status(3) == "relay 3: off"


def test_a_garbage_status_answer_is_a_bad_answer(pi: FakeBoilerPi, client: BoilerClient) -> None:
    pi.garbage_status = True
    with pytest.raises(BoilerBadAnswer):
        client.get_status()


# --- uploads ----------------------------------------------------------------------------------


def test_schedule_uploads_are_stored_under_their_file_names(
    pi: FakeBoilerPi, client: BoilerClient
) -> None:
    client.upload_boiler_schedule(serialize_boiler_schedule(SCHEDULE))
    client.upload_timer_schedule(serialize_timer_schedule(TIMER))
    assert pi.files["Cal.json"] == serialize_boiler_schedule(SCHEDULE)
    assert pi.files["TimeCal.json"] == serialize_timer_schedule(TIMER)
    assert [(u.path, u.filename) for u in pi.uploads] == [
        ("/updatecalander/", "Cal.json"),
        ("/updaterelaycalander/", "TimeCal.json"),
    ]


def test_each_upload_is_exactly_one_request(pi: FakeBoilerPi, client: BoilerClient) -> None:
    client.upload_boiler_schedule(serialize_boiler_schedule(SCHEDULE))
    assert pi.calls == [("POST", "/updatecalander/")]


def test_the_fake_rejects_files_the_services_could_not_read(
    pi: FakeBoilerPi, client: BoilerClient
) -> None:
    pretty = json.dumps(json.loads(serialize_boiler_schedule(SCHEDULE)), indent=2).encode()
    with pytest.raises(BoilerRejected, match="could not read"):
        client.upload_boiler_schedule(pretty)
    assert pi.files == {}


def test_the_fake_rejects_a_post_without_the_file_field(pi: FakeBoilerPi) -> None:
    with pi.session() as session:
        response = session.post(
            "http://MARELLYPI3:8000/updatecalander/", json={"somekey": "somevalue"}, timeout=5
        )
    assert response.status_code == 400
    assert "field 'file'" in response.text


def test_a_rejected_upload_stores_nothing(pi: FakeBoilerPi, client: BoilerClient) -> None:
    pi.reject_uploads_with = 503
    with pytest.raises(BoilerRejected) as caught:
        client.upload_boiler_schedule(serialize_boiler_schedule(SCHEDULE))
    assert caught.value.status_code == 503
    assert pi.files == {} and pi.uploads == []


def test_a_relay_manual_file_is_stored_under_its_own_name(
    pi: FakeBoilerPi, client: BoilerClient
) -> None:
    content = serialize_relay_manual(TIMER.entries)
    client.upload_manual(ManualUpload(relay_filename(2), content))
    assert pi.files["2Timermanual.json"] == content


# --- manual On / Off / Auto against the fake (assumptions, see the module docstring) -------------


def test_manual_on_makes_the_status_manual_and_auto_brings_it_back(
    pi: FakeBoilerPi, client: BoilerClient
) -> None:
    override = ManualOverride(ManualEntry(7, clock_time(19), Decimal("5.0"), Decimal("5.0")))
    client.upload_manual(manual_upload(override))
    status = client.get_status()
    assert (status.mode, status.target, status.minimum) == ("Manual", 5.0, 5.0)
    assert "manual.json" in pi.files

    client.upload_manual(manual_upload(build_auto()))
    status = client.get_status()
    assert (status.mode, status.target, status.minimum) == ("Auto", 3.0, 2.0)
    assert "manual.json" not in pi.files


# --- failure injection ------------------------------------------------------------------------


def test_offline_means_unreachable_and_coming_back_works(
    pi: FakeBoilerPi, client: BoilerClient
) -> None:
    pi.offline = True
    with pytest.raises(BoilerUnreachable):
        client.get_status()
    pi.offline = False
    assert client.get_status().mode == "Auto"


def test_a_slow_boiler_pi_times_out(pi: FakeBoilerPi, client: BoilerClient) -> None:
    pi.delay_s = 30.0  # more than the 5 s timeout; nothing really sleeps
    started = time.monotonic()
    with pytest.raises(BoilerUnreachable, match="timed out"):
        client.get_status()
    assert time.monotonic() - started < 1.0


def test_injected_http_errors_hit_only_the_next_requests(
    pi: FakeBoilerPi, client: BoilerClient
) -> None:
    pi.fail_next(2, status=500)
    for _ in range(2):
        with pytest.raises(BoilerRejected) as caught:
            client.get_status()
        assert caught.value.status_code == 500
    assert client.get_status().mode == "Auto"


def test_an_unknown_path_is_a_404(pi: FakeBoilerPi) -> None:
    with pi.session() as session:
        assert session.get("http://MARELLYPI3:8000/nothing/", timeout=5).status_code == 404


def test_the_fake_refuses_to_talk_to_any_other_host(pi: FakeBoilerPi) -> None:
    with pi.session() as session, pytest.raises(AssertionError, match="unexpected host"):
        session.get("http://192.168.0.10/status/", timeout=5)
