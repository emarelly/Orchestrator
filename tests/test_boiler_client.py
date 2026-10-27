"""Boiler Pi REST client (section 3.6, FR-PUSH-1..3, CS-9). No test touches a real network."""

import dataclasses
import json
import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
import requests

from orchestrator.boiler_client import (
    BoilerBadAnswer,
    BoilerClient,
    BoilerRejected,
    BoilerUnreachable,
    NotConfigured,
)
from orchestrator.config import BoilerConfig
from orchestrator.manual import ManualUpload
from tests.fakes import FakeBoilerAdapter, fake_session

BASE = "http://MARELLYPI3:8000/"
SENT = FakeBoilerAdapter.normalize  # requests lower-cases the host name when it sends
SENTINEL = "SENTINEL-FILE-CONTENT-4711"
SAMPLE_STATUS: dict[str, Any] = {
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


@pytest.fixture
def adapter() -> FakeBoilerAdapter:
    return FakeBoilerAdapter()


@pytest.fixture
def session(adapter: FakeBoilerAdapter) -> Iterator[requests.Session]:
    with fake_session(adapter) as session:
        yield session


def make_client(
    session: requests.Session, *, dry_run: bool = False, config: BoilerConfig | None = None
) -> BoilerClient:
    return BoilerClient(config or BoilerConfig(), session=session, dry_run=dry_run)


# --- reading status, history, log, relay ------------------------------------------------------


def test_status_is_read_from_the_configured_url_with_a_timeout(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    adapter.route("GET", BASE + "status/", body=json.dumps(SAMPLE_STATUS))
    status = make_client(session).get_status()
    assert status.mode == "Manual"
    assert status.temps == (58.06, 54.19, 61.44)
    assert [(r.method, r.url, r.timeout) for r in adapter.requests] == [
        ("GET", SENT(BASE + "status/"), 5.0)
    ]


def test_every_url_comes_from_the_configuration(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    config = dataclasses.replace(
        BoilerConfig(),
        base_url="http://10.0.0.5:9000/",
        status_path="state/",
        request_timeout_s=2.5,
    )
    adapter.route("GET", "http://10.0.0.5:9000/state/", body=json.dumps(SAMPLE_STATUS))
    make_client(session, config=config).get_status()
    assert adapter.requests[0].url == "http://10.0.0.5:9000/state/"
    assert adapter.requests[0].timeout == 2.5


@pytest.mark.parametrize(
    "error",
    [requests.ConnectionError("refused"), requests.Timeout("slow"), requests.TooManyRedirects()],
)
def test_network_problems_become_boiler_unreachable(
    adapter: FakeBoilerAdapter, session: requests.Session, error: Exception
) -> None:
    adapter.route("GET", BASE + "status/", error=error)
    with pytest.raises(BoilerUnreachable):
        make_client(session).get_status()


def test_a_timeout_is_reported_as_such(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    adapter.route("GET", BASE + "status/", error=requests.ReadTimeout("slow"))
    with pytest.raises(BoilerUnreachable, match="timed out"):
        make_client(session).get_status()


def test_an_http_error_status_is_rejected_with_a_short_snippet(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    adapter.route("GET", BASE + "status/", status=500, body="x" * 1000)
    with pytest.raises(BoilerRejected) as caught:
        make_client(session).get_status()
    assert caught.value.status_code == 500
    assert len(caught.value.snippet) == 200


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        "[1, 2, 3]",
        json.dumps({"Status": "fail to connect to Manager", "ActiveYesNo": "No"}),
        json.dumps({k: v for k, v in SAMPLE_STATUS.items() if k != "CurrentTemp"}),
    ],
)
def test_unusable_status_answers_are_bad_answers(
    adapter: FakeBoilerAdapter, session: requests.Session, body: str
) -> None:
    adapter.route("GET", BASE + "status/", body=body)
    with pytest.raises(BoilerBadAnswer):
        make_client(session).get_status()


def test_history_rows_are_read_with_a_one_based_month(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    rows = [
        [2026, 10, 24, 16, 40, 19, 3.43, 4.0, 4.0, 0, 4.92],
        [2026, 10, 24, 16, 45, 19, 3.5, 4.0, 4.0, 1, 5.0],
    ]
    adapter.route("GET", BASE + "history/", body=json.dumps(rows))
    samples = make_client(session).get_history()
    assert len(samples) == 2
    assert samples[0].at == datetime(2026, 10, 24, 16, 40, 19)  # month 10 = October
    assert (samples[0].showers_linear, samples[0].showers_thermo) == (3.43, 4.92)
    assert samples[1].heater == 1


def test_history_as_the_boiler_pi_sends_it_is_read(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    # Real answer: no outer brackets and zero-padded time fields, which is not valid JSON.
    body = (
        "[2026,10,25,11,06,30,2.2,3.5,3.0,0,3.7],[2026,10,26,00,02,17,1.7,1.8,4.0,0,2.0],"
        "[2026,10,26,09,41,00,2.0,3.5,3.0,1,2.9]"
    )
    adapter.route("GET", BASE + "history/", body=body)
    samples = make_client(session).get_history()
    assert [s.at.hour * 100 + s.at.minute for s in samples] == [1106, 2, 941]
    assert samples[0].at.second == 30
    assert (samples[1].minimum, samples[1].target, samples[1].showers_thermo) == (1.8, 4.0, 2.0)
    assert samples[2].heater == 1


def test_an_empty_history_is_no_samples(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    adapter.route("GET", BASE + "history/", body="")
    assert make_client(session).get_history() == ()


@pytest.mark.parametrize(
    "body", ["oops", "{}", "[[1, 2, 3]]", "[[2026, 13, 1, 0, 0, 0, 1, 1, 1, 0, 1]]"]
)
def test_bad_history_answers(
    adapter: FakeBoilerAdapter, session: requests.Session, body: str
) -> None:
    adapter.route("GET", BASE + "history/", body=body)
    with pytest.raises(BoilerBadAnswer):
        make_client(session).get_history()


def test_log_and_relay_status_are_returned_as_text(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    adapter.route("GET", BASE + "log/", body="line 1\nline 2")
    adapter.route("GET", BASE + "relay/?relay=2", body="relay 2 is off")
    client = make_client(session)
    assert client.get_log() == "line 1\nline 2"
    assert client.get_relay_status(2) == "relay 2 is off"


@pytest.mark.parametrize("relay", [-1, 8, True])
def test_relay_status_rejects_a_relay_that_does_not_exist(
    session: requests.Session, relay: int
) -> None:
    with pytest.raises(ValueError, match="relay must be"):
        make_client(session).get_relay_status(relay)


# --- uploads: exactly one POST ----------------------------------------------------------------


def test_the_boiler_schedule_is_one_multipart_post_and_nothing_else(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    adapter.route("POST", BASE + "updatecalander/", body="ok")
    result = make_client(session).upload_boiler_schedule(SENTINEL.encode())
    assert result.sent and result.status_code == 200 and result.text == "ok"
    assert len(adapter.requests) == 1  # the legacy Post() sent a second JSON request
    request = adapter.requests[0]
    assert request.method == "POST"
    assert request.timeout == 5.0
    assert request.content_type.startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="Cal.json"' in request.body
    assert SENTINEL.encode() in request.body
    assert request.body.count(b"Content-Disposition") == 1


def test_the_timer_schedule_goes_to_its_own_endpoint(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    adapter.route("POST", BASE + "updaterelaycalander/")
    make_client(session).upload_timer_schedule(b"[]")
    assert b'filename="TimeCal.json"' in adapter.requests[0].body


@pytest.mark.parametrize("status", [301, 302, 400, 404, 500])
def test_a_non_2xx_answer_to_an_upload_is_a_rejection_and_is_not_followed(
    adapter: FakeBoilerAdapter, session: requests.Session, status: int
) -> None:
    adapter.route(
        "POST", BASE + "updatecalander/", status=status, headers={"Location": BASE + "x/"}
    )
    with pytest.raises(BoilerRejected) as caught:
        make_client(session).upload_boiler_schedule(b"[]")
    assert caught.value.status_code == status
    assert len(adapter.requests) == 1  # no redirect was followed


def test_an_upload_that_cannot_connect_is_unreachable(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    adapter.route("POST", BASE + "updatecalander/", error=requests.ConnectionError("down"))
    with pytest.raises(BoilerUnreachable):
        make_client(session).upload_boiler_schedule(b"[]")


# --- manual files: undecided endpoints (OQ-16) ------------------------------------------------


def test_manual_uploads_are_refused_until_their_paths_are_configured(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    client = make_client(session)
    with pytest.raises(NotConfigured, match="manual_upload_path"):
        client.upload_manual(ManualUpload("manual.json", b"[]"))
    with pytest.raises(NotConfigured, match="relay_manual_upload_path"):
        client.upload_manual(ManualUpload("2Timermanual.json", b"[]"))
    assert adapter.requests == []  # nothing was sent anywhere


def test_manual_files_go_to_the_configured_paths(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    config = dataclasses.replace(
        BoilerConfig(), manual_upload_path="uploadmanual/", relay_manual_upload_path="uploadrelay/"
    )
    adapter.route("POST", BASE + "uploadmanual/")
    adapter.route("POST", BASE + "uploadrelay/")
    client = make_client(session, config=config)
    client.upload_manual(ManualUpload("manual.json", b"[]"))
    client.upload_manual(ManualUpload("2Timermanual.json", b'[{"x":1}]'))
    assert [r.url for r in adapter.requests] == [
        SENT(BASE + "uploadmanual/"),
        SENT(BASE + "uploadrelay/"),
    ]
    assert b'filename="manual.json"' in adapter.requests[0].body
    assert b'filename="2Timermanual.json"' in adapter.requests[1].body


# --- dry run and logging ----------------------------------------------------------------------


def test_dry_run_builds_but_never_sends_an_upload(
    adapter: FakeBoilerAdapter, session: requests.Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    config = dataclasses.replace(BoilerConfig(), manual_upload_path="uploadmanual/")
    client = make_client(session, dry_run=True, config=config)
    results = [
        client.upload_boiler_schedule(b"[]"),
        client.upload_timer_schedule(b"[]"),
        client.upload_manual(ManualUpload("manual.json", b"[]")),
    ]
    assert all(not r.sent and r.status_code == 0 for r in results)
    assert adapter.requests == []
    assert "reason=dry_run" in caplog.text


def test_dry_run_still_reads_the_status(
    adapter: FakeBoilerAdapter, session: requests.Session
) -> None:
    adapter.route("GET", BASE + "status/", body=json.dumps(SAMPLE_STATUS))
    assert make_client(session, dry_run=True).get_status().mode == "Manual"


def test_file_content_is_never_written_to_the_log(
    adapter: FakeBoilerAdapter, session: requests.Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter.route("POST", BASE + "updatecalander/", body="ok")
    make_client(session).upload_boiler_schedule(SENTINEL.encode())
    assert "boiler_upload" in caplog.text
    assert SENTINEL not in caplog.text
