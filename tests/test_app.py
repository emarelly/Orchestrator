"""The whole service started against fakes: start-up, workers, web, mail, MQTT, stop (OPS-2/3/5)."""

import dataclasses
import gc
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import orchestrator.app as app_module
from orchestrator.app import STOP_MARKER, Application, Overrides, main
from orchestrator.config import MailConfig, MqttConfig, load_config
from orchestrator.google_auth import (
    CALENDAR_READONLY_SCOPE,
    CALENDAR_SCOPE,
    GMAIL_SCOPE,
    write_token,
)
from tests.fake_boiler import FakeBoilerPi
from tests.fake_gmail import FakeGmailService, gmail_message
from tests.fake_google import FakeCalendarStore
from tests.fake_mqtt import FakeMqttLink
from tests.fakes import FakeClock
from tests.test_manual_commands import SATURDAY
from tests.test_ssh_reboot import Recording
from tests.test_supervisor import Recorder

ELI = "eli@gmail.com"
ADMIN = "admin@example.com"
ALERT = "alert@example.com"
SECRET = "pw-SENTINEL-app-8"


def wait_until(condition: Callable[[], bool], seconds: float = 6.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        google: bool = True,
        mqtt: bool = False,
        dry_run: bool = False,
        clock_check: Callable[[], bool | None] = lambda: True,
        clock_wait_s: float = 0.0,
        login: bool = False,
    ) -> None:
        self.pi = FakeBoilerPi()
        self.pi_facts: dict[str, Any] = {  # a healthy Pi, whatever the PC running the tests is like
            "memory_rss_mb": 50.0,
            "cpu_temp_c": 45.0,
            "disk_free_percent": 60.0,
            "clock_synchronized": True,
        }
        self.clock = FakeClock(SATURDAY)
        self.gmail = FakeGmailService("orchestrator@example.com")
        self.store = FakeCalendarStore(self.clock.now)
        self.link = FakeMqttLink()
        self.systemd = Recorder()
        base = load_config(None, env={})
        self.config = dataclasses.replace(
            base,
            general=dataclasses.replace(
                base.general,
                data_dir=str(tmp_path / "data"),
                log_dir=str(tmp_path / "logs"),
                dry_run=dry_run,
            ),
            boiler=dataclasses.replace(
                self.pi.config, poll_interval_s=0.05, down_after_failures=3, ssh_host_key=""
            ),
            calendar=dataclasses.replace(base.calendar, poll_interval_s=0.2),
            mail=MailConfig(
                user_email_list=(ELI,),
                admin_email=ADMIN,
                security_alert_email=ALERT,
                check_interval_s=0.05,
            ),
            mqtt=MqttConfig(enabled=mqtt, host="broker.local", state_interval_s=5),
            web=dataclasses.replace(base.web, host="127.0.0.1", login_required=login),
            secrets=dataclasses.replace(base.secrets, ui_password=SECRET if login else None),
        )
        self.overrides = Overrides(
            clock=self.clock,
            boiler_session=self.pi.session(),
            calendar_service=self.store if google else None,
            gmail_service=self.gmail if google else None,
            mqtt_link=self.link,
            ssh_connect=lambda host, port, timeout: Recording(),
            notifier=self.systemd,
            web_port=0,
            clock_check=clock_check,
            clock_wait_s=clock_wait_s,
            diagnostics_interval_s=0.2,
            facts=lambda: dict(self.pi_facts),
        )
        self.app = Application(self.config, overrides=self.overrides)
        self.thread_problems: list[str] = []
        self._previous_hook = threading.excepthook
        threading.excepthook = lambda args: self.thread_problems.append(repr(args.exc_value))

    def start(self) -> "Harness":
        self.app.start()
        return self

    def stop(self) -> None:
        self.app.stop()
        threading.excepthook = self._previous_hook
        session = self.overrides.boiler_session
        if session is not None:
            session.close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.app.web_port}{path}"

    def get(self, path: str) -> tuple[int, Any]:
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as answer:
                return answer.status, json.loads(answer.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def post(self, path: str, data: dict[str, Any]) -> tuple[int, Any]:
        request = urllib.request.Request(
            self.url(path),
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, json.loads(answer.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def sent_to(self, address: str) -> list[Any]:
        return [m for m in self.gmail.sent if m["To"] == address]


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    made = Harness(tmp_path).start()
    yield made
    made.stop()
    gc.collect()
    assert made.thread_problems == []


# --- start-up ---------------------------------------------------------------------------------


def test_the_service_starts_reports_ready_and_serves_the_page_and_the_status(
    harness: Harness,
) -> None:
    assert wait_until(lambda: "ready" in harness.systemd.events)
    assert wait_until(lambda: harness.get("/api/status")[1]["status"] is not None)
    code, health = harness.get("/healthz")
    assert code == 200 and health["status"] in ("ok", "degraded")
    assert set(health["components"]) == {
        "status-poller",
        "push-queue",
        "calendar",
        "mail",
        "diagnostics",
        "web",
    }
    assert health["version"] and "memory_rss_mb" in health["system"]
    with urllib.request.urlopen(harness.url("/"), timeout=5) as page:
        assert b"Home Control" in page.read()


def test_ready_waits_for_the_web_server_and_the_watchdog_is_pinged_while_healthy(
    harness: Harness,
) -> None:
    assert wait_until(lambda: harness.systemd.events.count("watchdog") >= 2)
    assert harness.systemd.events.count("ready") == 1
    assert "stopping" not in harness.systemd.events


def test_the_self_check_names_secrets_but_never_shows_a_value(tmp_path: Path) -> None:
    made = Harness(tmp_path, login=True).start()
    try:
        names = {c["check"]: c for c in made.app.self_check}
        assert set(names) == {"configuration", "secrets", "clock", "disk", "google", "dry run"}
        assert "UI_PASSWORD" in names["secrets"]["detail"]
        _, health = made.get("/healthz")
        _, system = made.get("/api/system")
        assert SECRET not in json.dumps(health) + json.dumps(system)
        assert health["system"]["self_check"][0]["check"] == "configuration"
    finally:
        made.stop()


def test_the_started_mail_goes_to_the_admin_with_the_self_check_and_the_previous_stop(
    tmp_path: Path,
) -> None:
    first = Harness(tmp_path).start()
    assert wait_until(lambda: bool(first.sent_to(ADMIN)))
    started = first.sent_to(ADMIN)[0]
    assert started["Subject"] == "Orchestrator: Orchestrator started"
    text = started.get_content()
    assert "Previous stop: unknown" in text and "configuration: ok" in text
    first.stop()
    assert (tmp_path / "data" / STOP_MARKER).exists()
    second = Harness(tmp_path).start()
    assert wait_until(lambda: bool(second.sent_to(ADMIN)))
    assert (
        "Previous stop: clean stop at 2026-10-24T16:41:00" in second.sent_to(ADMIN)[0].get_content()
    )
    second.stop()
    assert not any(first.thread_problems + second.thread_problems)


def test_nothing_is_mailed_at_start_without_an_admin_address(tmp_path: Path) -> None:
    made = Harness(tmp_path)
    made.app.config = dataclasses.replace(made.config, mail=MailConfig(user_email_list=(ELI,)))
    made.start()
    time.sleep(0.3)
    assert made.sent_to(ADMIN) == []
    made.stop()


# --- the workers do their jobs -------------------------------------------------------------------


def test_a_calendar_event_ends_up_as_a_schedule_file_on_the_boiler_pi(tmp_path: Path) -> None:
    made = Harness(tmp_path)
    made.store.seed(
        "b1",
        "Boiler: target=3.6; min=2",
        "2026-10-19T06:00:00+03:00",
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    )
    made.start()
    try:
        assert wait_until(lambda: "Cal.json" in made.pi.files)
        assert made.pi.files["Cal.json"] == (
            b'[{"DayOfWeek":2,"Hour":"06:00:00","Target":3.6,"Min":2}]'
        )
        assert (tmp_path / "data" / "Cal.json").exists()  # kept on disk for the next start (OPS-5)
        code, schedules = made.get("/api/schedules")
        assert code == 200 and len(schedules["items"]) == 1
    finally:
        made.stop()


def test_manual_on_from_the_page_reaches_the_boiler_pi(harness: Harness) -> None:
    assert wait_until(lambda: harness.get("/api/status")[1]["status"] is not None)
    code, answer = harness.post("/api/manual", {"action": "off"})
    assert code == 200 and answer["ok"] is True
    assert [name for name, _ in harness.pi.commands] == ["manual_off"]


def test_a_mail_command_is_answered_and_a_stranger_only_raises_an_alert(harness: Harness) -> None:
    harness.gmail.add(gmail_message("m1", ELI, "status", ""))
    harness.gmail.add(gmail_message("m2", "stranger@example.net", "on", ""))
    assert wait_until(lambda: bool(harness.sent_to(ELI)) and bool(harness.sent_to(ALERT)))
    assert harness.sent_to("stranger@example.net") == []
    assert harness.pi.uploads == [] and harness.pi.commands == []
    assert wait_until(lambda: harness.gmail.inbox == {})


def test_the_two_step_mail_exchange_works_through_the_whole_service(harness: Harness) -> None:
    assert wait_until(lambda: harness.get("/api/status")[1]["status"] is not None)
    harness.gmail.add(gmail_message("m1", ELI, "off", ""))
    assert wait_until(lambda: any(len(m["Subject"]) == 10 for m in harness.sent_to(ELI)))
    token = next(m["Subject"] for m in harness.sent_to(ELI) if len(m["Subject"]) == 10)
    harness.gmail.add(gmail_message("m2", ELI, token, ""))
    assert wait_until(lambda: ("manual_off", None) in harness.pi.commands)


def test_a_boiler_that_goes_offline_and_returns_is_reported_to_the_admin(harness: Harness) -> None:
    assert wait_until(lambda: harness.get("/api/status")[1]["status"] is not None)
    harness.pi.offline = True
    assert wait_until(
        lambda: any("not responding" in m["Subject"] for m in harness.sent_to(ADMIN)), 10
    )

    def poller_state() -> tuple[int, str]:
        code, health = harness.get("/healthz")
        return code, health["components"]["status-poller"]["state"]

    # the supervisor looks about every 5 s; an offline boiler is degraded, never a failure
    assert wait_until(lambda: poller_state() == (200, "degraded"), 12)
    harness.pi.offline = False
    assert wait_until(
        lambda: any("Boiler Pi is back" in m["Subject"] for m in harness.sent_to(ADMIN)), 10
    )


# --- MQTT --------------------------------------------------------------------------------------


def test_mqtt_is_started_only_when_enabled_and_works_end_to_end(tmp_path: Path) -> None:
    off = Harness(tmp_path / "a").start()
    try:
        assert "mqtt" not in off.get("/healthz")[1]["components"]
        assert not off.link.started
    finally:
        off.stop()
    on = Harness(tmp_path / "b", mqtt=True).start()
    try:
        assert wait_until(lambda: on.link.started)
        on.link.connect()
        assert wait_until(lambda: "orchestrator/state" in on.link.retained)
        diag = json.loads(on.link.retained["orchestrator/state"])
        assert diag["version"] and diag["failed_components"] == 0 and "uptime_min" in diag
        on.link.receive("orchestrator/cmd/manual", "off")
        assert wait_until(lambda: ("manual_off", None) in on.pi.commands)
        assert on.get("/healthz")[1]["components"]["mqtt"]["state"] in ("ok", "degraded")
    finally:
        on.stop()
    assert on.link.stopped and on.link.retained["orchestrator/availability"] == "offline"


# --- missing Google access -------------------------------------------------------------------


def test_without_google_access_the_service_still_runs_and_says_what_is_needed(
    tmp_path: Path,
) -> None:
    made = Harness(tmp_path, google=False).start()
    try:
        assert wait_until(
            lambda: (made.get("/api/schedules")[1]["calendar"] or {}).get("auth_lost") is True
        )
        needed = "Google authorization needed"  # the supervisor looks about every 5 s
        assert wait_until(
            lambda: made.get("/healthz")[1]["components"]["mail"]["detail"] == needed, 12
        )
        code, health = made.get("/healthz")
        assert code == 200 and health["status"] == "degraded"
        assert health["components"]["calendar"]["detail"] == needed
        assert health["components"]["mail"]["detail"] == needed
        assert made.get("/api/status")[0] == 200  # the boiler side is not affected
        check = next(c for c in made.app.self_check if c["check"] == "google")
        assert check["ok"] is False and "GOOGLE_TOKEN_FILE" in check["detail"]
        code, _ = made.post(
            "/api/schedules/boiler", {"days": [2], "time": "06:00", "target": 3, "min": 2}
        )
        assert code == 503  # editing needs the calendar
    finally:
        made.stop()


# --- the clock ------------------------------------------------------------------------------


def test_no_schedule_is_sent_to_the_boiler_pi_while_the_clock_is_unsynchronized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "CLOCK_POLL_S", 0.05)
    synchronized = [False]
    made = Harness(tmp_path, clock_check=lambda: synchronized[0], clock_wait_s=0.1)
    made.store.seed(
        "b1",
        "Boiler: target=3.6; min=2",
        "2026-10-19T06:00:00+03:00",
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    )
    made.start()
    try:
        assert wait_until(lambda: made.get("/api/status")[1]["status"] is not None)
        time.sleep(0.5)
        assert made.pi.uploads == []  # no schedule yet
        assert next(c for c in made.app.self_check if c["check"] == "clock")["ok"] is False
        # a manual command is carried out by the boiler Pi with its own clock, so it may go
        assert made.post("/api/manual", {"action": "off"})[0] == 200
        synchronized[0] = True
        assert wait_until(lambda: "Cal.json" in made.pi.files)
    finally:
        made.stop()


def test_an_unknown_clock_state_does_not_hold_anything_back(tmp_path: Path) -> None:
    made = Harness(tmp_path, clock_check=lambda: None).start()  # e.g. not systemd
    try:
        assert wait_until(lambda: made.get("/api/status")[1]["status"] is not None)
        assert made.post("/api/manual", {"action": "auto"})[0] == 200
    finally:
        made.stop()


# --- dry run ---------------------------------------------------------------------------------


def test_in_a_dry_run_nothing_is_uploaded_to_the_boiler_pi(tmp_path: Path) -> None:
    made = Harness(tmp_path, dry_run=True)
    made.store.seed(
        "b1",
        "Boiler: target=3.6; min=2",
        "2026-10-19T06:00:00+03:00",
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    )
    made.start()
    try:
        assert wait_until(lambda: made.get("/api/status")[1]["status"] is not None)
        code, answer = made.post("/api/manual", {"action": "off"})
        assert code == 200 and "dry run" in answer["message"]
        assert wait_until(lambda: (tmp_path / "data" / "Cal.json").exists())
        time.sleep(0.3)
        assert made.pi.uploads == [] and made.pi.commands == []
    finally:
        made.stop()


# --- stopping and the command line -------------------------------------------------------------


def test_stopping_ends_every_thread_and_can_be_repeated(tmp_path: Path) -> None:
    before = {t.name for t in threading.enumerate()}
    made = Harness(tmp_path, mqtt=True).start()
    made.link.connect()
    made.stop()
    made.app.stop()
    leftover = {t.name for t in threading.enumerate()} - before
    assert leftover == set()
    assert "stopping" in made.systemd.events
    assert made.thread_problems == []


def test_a_bad_configuration_stops_with_a_clear_message_and_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[web]\nport = 70000\n", encoding="utf-8")
    import io

    out = io.StringIO()
    assert main(["--config", str(bad)], env={}, stream=out) == 2
    assert out.getvalue().startswith("configuration error: [web] port")
    assert main(["--config", str(tmp_path / "missing.toml")], env={}, stream=out) == 2
    assert capsys.readouterr().out == ""


# --- one Google login for the mail and another for the calendar ----------------------------


def write_login(path: Path, scope: str) -> str:
    write_token(
        path,
        {
            "type": "authorized_user",
            "client_id": "1.apps.googleusercontent.com",
            "client_secret": "s",
            "refresh_token": "r-" + path.stem,
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": [scope],
        },
    )
    return str(path)


class GoogleSetup:
    """An application whose Google services are built from token files (the builders are fakes)."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        mail: str | None = GMAIL_SCOPE,
        calendar: str | None = CALENDAR_READONLY_SCOPE,
        calendar_file: bool = True,
        write_back: bool = True,
    ) -> None:
        self.store = FakeCalendarStore(FakeClock(SATURDAY).now)
        self.gmail = FakeGmailService("orchestrator@example.com")
        monkeypatch.setattr(app_module, "build_calendar_service", lambda credentials: self.store)
        monkeypatch.setattr(app_module, "build_mail_service", lambda credentials: self.gmail)
        folder = tmp_path / "google"
        folder.mkdir()
        main = write_login(folder / "token.json", mail) if mail else str(folder / "token.json")
        own = str(folder / "calendar_token.json")
        if calendar and calendar_file:
            write_login(folder / "calendar_token.json", calendar)
        base = load_config(None, env={})
        self.config = dataclasses.replace(
            base,
            general=dataclasses.replace(
                base.general, data_dir=str(tmp_path / "data"), dry_run=True
            ),
            boiler=dataclasses.replace(FakeBoilerPi().config, poll_interval_s=0.05),
            calendar=dataclasses.replace(base.calendar, write_back=write_back),
            web=dataclasses.replace(base.web, host="127.0.0.1"),
            secrets=dataclasses.replace(
                base.secrets, google_token_file=main, google_calendar_token_file=own
            ),
        )

    def application(self) -> Application:
        return Application(
            self.config,
            overrides=Overrides(
                web_port=0,
                clock=FakeClock(SATURDAY),
                boiler_session=FakeBoilerPi().session(),
                mqtt_link=FakeMqttLink(),
                notifier=Recorder(),
                clock_check=lambda: True,
                clock_wait_s=0.0,
                diagnostics_interval_s=0.2,
                facts=lambda: {},
            ),
        )

    def access(self) -> Any:
        return self.application()._google_access()


def test_the_mail_and_the_calendar_may_use_two_different_logins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access = GoogleSetup(tmp_path, monkeypatch).access()
    assert access.mail is not None and access.calendar is not None
    assert access.mail_note == "" and access.calendar_note == ""
    assert access.calendar_writable is False  # the old calendar login is read-only
    assert access.detail == "mail ok; calendar read-only"


def test_one_full_login_for_both_still_works_and_allows_page_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = GoogleSetup(tmp_path, monkeypatch, calendar=None)
    write_token(
        Path(setup.config.secrets.google_token_file or ""),
        {
            "type": "authorized_user",
            "client_id": "1",
            "client_secret": "s",
            "refresh_token": "r",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": [GMAIL_SCOPE, CALENDAR_SCOPE],
        },
    )
    access = setup.access()
    assert access.mail is not None and access.calendar is not None
    assert access.calendar_writable is True and access.detail == "mail ok; calendar read-write"


def test_without_a_separate_calendar_file_the_main_login_is_used_for_the_calendar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access = GoogleSetup(tmp_path, monkeypatch, calendar_file=False).access()
    assert access.mail is not None
    assert access.calendar is None  # the mail login has no calendar permission
    assert "calendar: scopes_missing" in access.calendar_note


def test_a_login_with_the_wrong_permission_is_refused_for_that_job_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access = GoogleSetup(tmp_path, monkeypatch, mail=CALENDAR_READONLY_SCOPE).access()
    assert access.mail is None and "mail: scopes_missing" in access.mail_note
    assert access.calendar is not None  # the calendar still works


def test_a_missing_login_names_the_setting_to_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access = GoogleSetup(tmp_path, monkeypatch, mail=None, calendar=None).access()
    assert access.mail is None and access.calendar is None
    assert "missing" in access.mail_note and "missing" in access.calendar_note


def test_a_read_only_calendar_login_keeps_the_page_from_editing_even_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = GoogleSetup(tmp_path, monkeypatch, write_back=True).application()
    application.start()
    try:
        url = f"http://127.0.0.1:{application.web_port}/api/schedules"
        with urllib.request.urlopen(url, timeout=5) as answer:
            assert json.loads(answer.read())["editing_enabled"] is False
        check = next(c for c in application.self_check if c["check"] == "google")
        assert check["ok"] is True and check["detail"] == "mail ok; calendar read-only"
    finally:
        application.stop()
