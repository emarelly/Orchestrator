"""The web API: every endpoint and error case (CS-15, FR-UI-1..7, OPS-8)."""

import dataclasses
import gc
import gzip
import json
import re
import threading
import urllib.request
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from flask.testing import FlaskClient
from werkzeug.exceptions import HTTPException

from orchestrator.config import Config, load_config
from orchestrator.models import HistorySample
from orchestrator.push_queue import Outcome, PushResult, ScheduleKind
from orchestrator.web_api import (
    LOGIN_WINDOW_S,
    MAX_BODY_BYTES,
    MAX_FAILED_LOGINS,
    UI_FILE,
    HistoryProvider,
    LoginLimiter,
    WebDeps,
    create_app,
    make_server,
)
from tests.fakes import FakeClock
from tests.test_calendar_sync import NOW, World
from tests.test_manual_commands import Rig

PASSWORD = "Pa55-w0rd-SENTINEL"
SAME_ORIGIN = {"Origin": "http://localhost"}


class Site:
    """The API over a fake boiler Pi and a fake calendar."""

    def __init__(
        self,
        *,
        login: bool = False,
        editing: bool = True,
        history: Callable[[], tuple[HistorySample, ...]] | None = None,
        poll: bool = True,
    ) -> None:
        self.rig = Rig(start=NOW, poll_first=poll)
        self.world = World()
        env = {"UI_PASSWORD": PASSWORD} if login else {}
        base = load_config(None, env=env)
        self.config: Config = dataclasses.replace(
            base,
            web=dataclasses.replace(base.web, login_required=login),
            calendar=dataclasses.replace(base.calendar, write_back=editing),
        )
        self.history_calls = 0
        self.fetch = history or self._history
        self.deps = WebDeps(
            config=self.config,
            clock=self.rig.clock,
            cache=self.rig.cache,
            manual=self.rig.commands,
            history=HistoryProvider(self._counted, self.rig.clock.monotonic),
            sync=self.world.sync,
            editor=self.world.editor if editing else None,
            log_lines=lambda: ["line one", "line two"],
        )
        self.app = create_app(self.deps)
        self.client: FlaskClient = self.app.test_client()

    def _history(self) -> tuple[HistorySample, ...]:
        return self.rig.client.get_history()

    def _counted(self) -> tuple[HistorySample, ...]:
        self.history_calls += 1
        return self.fetch()

    def post(self, url: str, data: Any, **kwargs: Any) -> Any:
        return self.client.post(url, json=data, headers=SAME_ORIGIN, **kwargs)

    def put(self, url: str, data: Any) -> Any:
        return self.client.put(url, json=data, headers=SAME_ORIGIN)

    def delete(self, url: str, data: Any | None = None) -> Any:
        return self.client.delete(url, json=data or {}, headers=SAME_ORIGIN)

    def login(self, password: str = PASSWORD) -> Any:
        return self.post("/api/login", {"password": password})

    def boiler_posts(self) -> int:
        return len(self.rig.posts())


@pytest.fixture
def site() -> Site:
    return Site()


# --- status and history -----------------------------------------------------------------------


def test_status_shows_the_mode_levels_and_age(site: Site) -> None:
    answer = site.client.get("/api/status")
    assert answer.status_code == 200
    data = answer.get_json()
    assert data["connection"]["state"] == "up"
    assert data["stale"] is False
    reading = data["status"]
    assert reading["mode"] in ("Auto", "Manual", "Off")
    levels = reading["levels"]
    assert set(levels) == {"upper", "mid", "lower"}  # fixed sensor positions (FR-UI-1b)
    assert reading["age_s"] >= 0
    assert data["dry_run"] is False


def test_status_before_the_first_reading_is_empty_and_stale() -> None:
    site = Site(poll=False)
    data = site.client.get("/api/status").get_json()
    assert data["status"] is None
    assert data["stale"] is True
    assert data["connection"]["state"] == "unknown"


def test_an_old_reading_is_flagged_stale(site: Site) -> None:
    site.rig.clock.advance(timedelta(minutes=10))
    assert site.client.get("/api/status").get_json()["stale"] is True


def test_history_is_cached_for_a_minute_and_refreshed_after(site: Site) -> None:
    site.rig.pi.history_rows = [[2026, 10, 24, 16, 0, 0, 1.5, 1, 2, 0, 1.6]]
    first = site.client.get("/api/history").get_json()
    site.client.get("/api/history")
    assert site.history_calls == 1
    site.rig.clock.advance(timedelta(seconds=61))
    site.client.get("/api/history")
    assert site.history_calls == 2
    assert first["error"] is None
    assert first["samples"][0]["at"] == "2026-10-24T16:00:00"
    assert first["samples"][0]["showers_thermo"] == 1.6


def test_a_failing_history_keeps_the_last_good_samples_and_says_so() -> None:
    calls = {"n": 0}
    good = HistorySample(
        at=NOW.replace(tzinfo=None),
        showers_linear=1,
        minimum=1,
        target=2,
        heater=0,
        showers_thermo=1,
    )

    def flaky() -> tuple[HistorySample, ...]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("boiler Pi down")
        return (good,)

    site = Site(history=flaky)
    assert len(site.client.get("/api/history").get_json()["samples"]) == 1
    site.rig.clock.advance(timedelta(seconds=120))
    again = site.client.get("/api/history").get_json()
    assert len(again["samples"]) == 1
    assert again["error"] == "OSError"
    assert "boiler Pi down" not in json.dumps(again)


# --- schedules --------------------------------------------------------------------------------


def test_schedules_are_empty_before_the_first_calendar_reading(site: Site) -> None:
    data = site.client.get("/api/schedules").get_json()
    assert data["calendar"] is None
    assert data["items"] == [] and data["boiler"] == [] and data["timer"] == []
    assert data["editing_enabled"] is True


def create_morning(site: Site, **overrides: Any) -> Any:
    body = {"days": [2, 4], "time": "06:00", "target": 3.6, "min": 2}
    return site.post("/api/schedules/boiler", {**body, **overrides})


def test_a_created_item_appears_after_the_next_sync_with_its_id(site: Site) -> None:
    answer = create_morning(site)
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json()["outcome"] == "applied"
    site.world.sync.sync_once()
    data = site.client.get("/api/schedules").get_json()
    assert [(s["day"], s["start"], s["target"], s["min"]) for s in data["boiler"]] == [
        (2, "06:00:00", "3.6", "2"),
        (4, "06:00:00", "3.6", "2"),
    ]
    assert {item["kind"] for item in data["items"]} == {"boiler"}
    assert all(item["id"] for item in data["items"])
    assert data["calendar"]["ok"] is True


def test_an_item_can_be_changed_and_deleted_by_its_id(site: Site) -> None:
    create_morning(site, days=[2])
    site.world.sync.sync_once()
    item = site.client.get("/api/schedules").get_json()["items"][0]
    url = "/api/schedules/boiler/" + item["id"]
    changed = site.put(url, {"target": 4.5, "min": 2})
    assert changed.status_code == 200, changed.get_json()
    site.world.sync.sync_once()
    again = site.client.get("/api/schedules").get_json()
    assert again["boiler"][0]["target"] == "4.5"
    assert site.delete("/api/schedules/boiler/" + again["items"][0]["id"]).status_code == 200
    site.world.sync.sync_once()
    assert site.client.get("/api/schedules").get_json()["items"] == []


def test_a_value_above_the_limit_is_refused_and_nothing_is_written(site: Site) -> None:
    answer = create_morning(site, target=6)
    assert answer.status_code == 422
    assert answer.get_json()["outcome"] == "refused"
    assert site.world.store.writes() == []


def test_a_timer_item_can_be_created_with_an_astro_start(site: Site) -> None:
    answer = site.post(
        "/api/schedules/timer",
        {
            "days": [2],
            "relays": [4],
            "start_astro": {"astro": "sunset", "offset": 30},
            "end": "23:00",
        },
    )
    assert answer.status_code == 200, answer.get_json()
    assert site.world.store.writes() == [("insert", None)]


@pytest.mark.parametrize(
    ("kind", "body", "fragment"),
    [
        ("boiler", {"days": [8], "time": "06:00", "target": 3, "min": 2}, "days"),
        ("boiler", {"days": [], "time": "06:00", "target": 3, "min": 2}, "days"),
        ("boiler", {"days": [1, 1], "time": "06:00", "target": 3, "min": 2}, "twice"),
        ("boiler", {"days": [1], "time": "6am", "target": 3, "min": 2}, "time"),
        ("boiler", {"days": [1], "target": 3, "min": 2}, "time"),
        ("boiler", {"days": [1], "time": "06:00", "target": "x", "min": 2}, "target"),
        ("boiler", {"days": [1], "time": "06:00", "target": True, "min": 2}, "target"),
        ("boiler", {"days": [1], "time": "06:00", "target": 3}, "min"),
        ("boiler", {"days": [1], "time": "06:00", "target": 3, "min": 2, "bogus": 1}, "bogus"),
        ("timer", {"days": [1], "time": "06:00", "relays": [], "end": "23:00"}, "relays"),
        ("timer", {"days": [1], "time": "06:00", "relays": [4]}, "exactly one"),
        (
            "timer",
            {"days": [1], "time": "06:00", "relays": [4], "end": "23:00", "duration_s": 5},
            "exactly one",
        ),
        ("timer", {"days": [1], "relays": [4], "end": "23:00"}, "time"),
        ("timer", {"days": [1], "time": "06:00", "relays": [4], "duration_s": 0}, "duration_s"),
        (
            "timer",
            {"days": [1], "time": "06:00", "relays": [4], "end_astro": {"astro": "noon!"}},
            "astro",
        ),
    ],
)
def test_bad_schedule_input_is_a_400_that_names_the_field(
    site: Site, kind: str, body: dict[str, Any], fragment: str
) -> None:
    answer = site.post("/api/schedules/" + kind, body)
    assert answer.status_code == 400
    assert fragment in answer.get_json()["error"]
    assert site.world.store.writes() == []


def test_unknown_kinds_and_items_are_404(site: Site) -> None:
    assert site.post("/api/schedules/heater", {}).status_code == 404
    site.world.sync.sync_once()
    assert site.put("/api/schedules/boiler/nope", {"target": 3, "min": 2}).status_code == 404
    assert site.delete("/api/schedules/boiler/nope").status_code == 404


def test_an_item_of_the_other_kind_is_not_found_under_this_kind(site: Site) -> None:
    create_morning(site, days=[2])
    site.world.sync.sync_once()
    item_id = site.client.get("/api/schedules").get_json()["items"][0]["id"]
    assert site.delete("/api/schedules/timer/" + item_id).status_code == 404


def test_editing_is_unavailable_without_an_editor() -> None:
    site = Site(editing=False)
    assert site.client.get("/api/schedules").get_json()["editing_enabled"] is False
    assert create_morning(site).status_code == 503


def test_a_stale_edit_is_a_409_conflict(site: Site) -> None:
    create_morning(site, days=[2])
    site.world.sync.sync_once()
    item = site.client.get("/api/schedules").get_json()["items"][0]
    site.world.store.touch(item["id"].split("_")[0], NOW + timedelta(minutes=5))
    answer = site.put(
        "/api/schedules/boiler/" + item["id"],
        {"target": 4, "min": 2, "edited_at": NOW.isoformat()},
    )
    assert answer.status_code == 409
    assert answer.get_json()["outcome"] == "conflict"


def test_edited_at_must_have_a_time_zone(site: Site) -> None:
    create_morning(site, days=[2])
    site.world.sync.sync_once()
    item_id = site.client.get("/api/schedules").get_json()["items"][0]["id"]
    answer = site.put(
        "/api/schedules/boiler/" + item_id,
        {"target": 4, "min": 2, "edited_at": "2026-10-24T10:00:00"},
    )
    assert answer.status_code == 400


def test_sync_now_asks_the_calendar_loop_to_run(site: Site) -> None:
    assert site.post("/api/sync", {}).status_code == 202


# --- manual control ---------------------------------------------------------------------------


def test_manual_on_off_and_auto_upload_one_file_each(site: Site) -> None:
    for action in ("on", "off", "auto"):
        answer = site.post("/api/manual", {"action": action})
        assert answer.status_code == 200, answer.get_json()
        assert answer.get_json()["ok"] is True
    assert site.boiler_posts() == 3


def test_manual_rejects_unknown_actions_and_fields(site: Site) -> None:
    assert site.post("/api/manual", {"action": "boil"}).status_code == 400
    assert site.post("/api/manual", {"action": "on", "target": 9}).status_code == 400
    assert site.post("/api/manual", {}).status_code == 400
    assert site.boiler_posts() == 0


def test_manual_on_is_refused_when_the_status_is_not_known() -> None:
    site = Site(poll=False)
    answer = site.post("/api/manual", {"action": "on"})
    assert answer.status_code == 409
    assert answer.get_json()["ok"] is False
    assert site.boiler_posts() == 0


def test_a_relay_can_be_run_and_stopped(site: Site) -> None:
    assert site.post("/api/relays/4", {"action": "on", "seconds": 600}).status_code == 200
    assert site.post("/api/relays/4", {"action": "off"}).status_code == 200


@pytest.mark.parametrize("relay", [0, 1, 8, 99])
def test_the_boilers_own_relays_and_unknown_relays_are_never_commanded(
    site: Site, relay: int
) -> None:
    answer = site.post(f"/api/relays/{relay}", {"action": "on", "seconds": 60})
    assert answer.status_code == 409
    assert site.boiler_posts() == 0


@pytest.mark.parametrize(
    "body",
    [
        {"action": "on"},
        {"action": "on", "seconds": 0},
        {"action": "on", "seconds": "60"},
        {"action": "on", "seconds": 86401},
        {"action": "spin"},
    ],
)
def test_bad_relay_input_is_a_400(site: Site, body: dict[str, Any]) -> None:
    assert site.post("/api/relays/4", body).status_code == 400


def test_the_relay_list_marks_relays_0_and_1_as_the_boilers_own(site: Site) -> None:
    relays = site.client.get("/api/relays").get_json()["relays"]
    assert [r["number"] for r in relays] == list(range(8))
    assert [r["number"] for r in relays if r["locked"]] == [0, 1]
    assert relays[4]["name"] == "GardenLights"


# --- request safety (CS-15) -------------------------------------------------------------------


def test_a_request_from_another_site_is_refused(site: Site) -> None:
    answer = site.client.post(
        "/api/manual", json={"action": "on"}, headers={"Origin": "http://evil.example"}
    )
    assert answer.status_code == 403
    assert site.boiler_posts() == 0


def test_the_literal_null_origin_is_refused(site: Site) -> None:
    answer = site.client.post("/api/manual", json={"action": "on"}, headers={"Origin": "null"})
    assert answer.status_code == 403


def test_a_request_without_an_origin_header_is_fine_when_it_is_json(site: Site) -> None:
    assert site.client.post("/api/manual", json={"action": "auto"}).status_code == 200


def test_form_posts_are_refused(site: Site) -> None:
    answer = site.client.post("/api/manual", data="action=on", headers=SAME_ORIGIN)
    assert answer.status_code == 415
    assert site.boiler_posts() == 0


def test_a_body_that_is_not_json_is_a_400(site: Site) -> None:
    answer = site.client.post(
        "/api/manual", data="{nope", content_type="application/json", headers=SAME_ORIGIN
    )
    assert answer.status_code == 400
    answer = site.client.post(
        "/api/manual", data="[1]", content_type="application/json", headers=SAME_ORIGIN
    )
    assert answer.status_code == 400
    assert site.boiler_posts() == 0


def test_a_large_body_is_refused_with_413(site: Site) -> None:
    answer = site.post("/api/manual", {"action": "x" * (MAX_BODY_BYTES + 1)})
    assert answer.status_code == 413


def test_answers_are_not_cached_and_carry_safety_headers(site: Site) -> None:
    answer = site.client.get("/api/status")
    assert answer.headers["Cache-Control"] == "no-store"
    assert answer.headers["X-Content-Type-Options"] == "nosniff"
    assert answer.headers["X-Frame-Options"] == "DENY"


def test_unknown_paths_and_methods_are_json_errors(site: Site) -> None:
    assert site.client.get("/api/nothing").get_json() == {"error": "not found"}
    assert site.client.delete("/api/status", headers=SAME_ORIGIN, json={}).status_code == 405


def test_an_unexpected_error_is_a_500_without_details(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*, source: str, who: str) -> Any:
        raise RuntimeError("secret internals " + PASSWORD)

    monkeypatch.setattr(site.rig.commands, "manual_on", boom)
    answer = site.post("/api/manual", {"action": "on"})
    assert answer.status_code == 500
    assert answer.get_json() == {"error": "internal error"}


# --- login (FR-UI-7) --------------------------------------------------------------------------


def test_without_login_configured_everything_is_open_and_login_is_unavailable(site: Site) -> None:
    assert site.client.get("/api/session").get_json() == {
        "login_required": False,
        "logged_in": True,
    }
    assert site.login().status_code == 404


def test_actions_need_a_login_but_reading_does_not() -> None:
    site = Site(login=True)
    assert site.client.get("/api/status").status_code == 200
    assert site.client.get("/api/session").get_json() == {
        "login_required": True,
        "logged_in": False,
    }
    assert site.post("/api/manual", {"action": "on"}).status_code == 401
    assert create_morning(site).status_code == 401
    assert site.post("/api/sync", {}).status_code == 401
    assert site.boiler_posts() == 0


def test_a_wrong_password_is_refused_and_the_right_one_opens_the_actions() -> None:
    site = Site(login=True)
    assert site.login("wrong").status_code == 401
    answer = site.login()
    assert answer.status_code == 200
    assert PASSWORD not in answer.get_data(as_text=True)
    assert site.client.get("/api/session").get_json()["logged_in"] is True
    assert site.post("/api/manual", {"action": "auto"}).status_code == 200


def test_logout_ends_the_session() -> None:
    site = Site(login=True)
    site.login()
    assert site.post("/api/logout", {}).status_code == 200
    assert site.post("/api/manual", {"action": "auto"}).status_code == 401


def test_the_session_cookie_is_not_readable_by_scripts_or_other_sites() -> None:
    site = Site(login=True)
    cookie = site.login().headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie


def test_login_with_a_non_string_password_is_refused() -> None:
    site = Site(login=True)
    assert site.post("/api/login", {"password": None}).status_code == 401
    assert site.post("/api/login", {"password": ["x"]}).status_code == 401


def test_repeated_wrong_passwords_lock_the_address_out_for_a_while() -> None:
    site = Site(login=True)
    for _ in range(MAX_FAILED_LOGINS):
        assert site.login("wrong").status_code == 401
    assert site.login().status_code == 429  # even the right password waits
    site.rig.clock.advance(timedelta(seconds=LOGIN_WINDOW_S + 1))
    assert site.login().status_code == 200


def test_the_limiter_counts_per_address_and_forgets_after_success() -> None:
    clock = FakeClock(NOW)
    limiter = LoginLimiter(clock.monotonic)
    for _ in range(MAX_FAILED_LOGINS):
        limiter.failed("10.0.0.1")
    assert limiter.blocked("10.0.0.1")
    assert not limiter.blocked("10.0.0.2")
    limiter.succeeded("10.0.0.1")
    assert not limiter.blocked("10.0.0.1")


# --- the real server (waitress) ---------------------------------------------------------------


def test_the_app_is_served_by_waitress_over_a_real_socket(site: Site) -> None:
    server = make_server(site.app, "127.0.0.1", 0, threads=2)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.effective_port}"
        with urllib.request.urlopen(base + "/healthz", timeout=5) as answer:
            assert answer.status == 200
            assert json.loads(answer.read()) == {"status": "ok"}
        request = urllib.request.Request(
            base + "/api/manual",
            data=json.dumps({"action": "auto"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as answer:
            assert json.loads(answer.read())["ok"] is True
        assert site.boiler_posts() == 1
    finally:
        server.close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_stopping_the_server_right_after_a_request_never_raises_in_any_thread(site: Site) -> None:
    # regression: waitress closed its sockets while a worker thread was still finishing, which
    # raised OSError (WinError 10038 / EBADF) in a thread about 2 times in 100 stops
    problems: list[str] = []
    previous = threading.excepthook
    threading.excepthook = lambda args: problems.append(repr(args.exc_value))
    try:
        for _ in range(80):
            server = make_server(site.app, "127.0.0.1", 0, threads=2)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.effective_port}/healthz"
            with urllib.request.urlopen(url, timeout=5) as answer:
                answer.read()
            server.close()
            thread.join(timeout=5)
            assert not thread.is_alive()
    finally:
        threading.excepthook = previous
    gc.collect()  # a socket left open would be reported now, as a warning that fails this test
    assert problems == []


# --- system and health ------------------------------------------------------------------------


def test_the_system_page_has_version_log_and_no_secrets() -> None:
    site = Site(login=True)
    data = site.client.get("/api/system").get_json()
    assert data["version"]
    assert data["log"] == ["line one", "line two"]
    assert data["connection"] == "up"
    assert PASSWORD not in json.dumps(data)


def test_healthz_follows_the_supervisor_and_is_open_to_everyone() -> None:
    site = Site(login=True)  # no login needed, even when actions need one
    assert site.client.get("/healthz").get_json() == {"status": "ok"}

    def with_status(value: str) -> int:
        deps = dataclasses.replace(site.deps, health=lambda: {"status": value})
        return create_app(deps).test_client().get("/healthz").status_code

    assert with_status("ok") == 200 and with_status("degraded") == 200
    assert with_status("failed") == 503


def test_the_system_page_carries_the_same_health_data() -> None:
    site = Site()
    deps = dataclasses.replace(site.deps, health=lambda: {"status": "degraded", "x": 1})
    data = create_app(deps).test_client().get("/api/system").get_json()
    assert data["health"] == {"status": "degraded", "x": 1}


def test_the_editor_is_the_only_writer_to_the_calendar(site: Site) -> None:
    site.client.get("/api/schedules")
    site.client.get("/api/status")
    site.client.get("/api/system")
    assert site.world.store.writes() == []


# --- push now and the series information the page needs -------------------------------------


def test_push_now_reports_each_file_and_needs_the_function_to_be_wired(site: Site) -> None:
    assert site.post("/api/push", {}).status_code == 503
    wired = dataclasses.replace(
        site.deps,
        push_now=lambda: [
            PushResult(ScheduleKind.BOILER, Outcome.PUSHED),
            PushResult(ScheduleKind.TIMER, Outcome.FAILED, "boiler Pi down"),
        ],
    )
    answer = create_app(wired).test_client().post("/api/push", json={}, headers=SAME_ORIGIN)
    data = answer.get_json()
    assert answer.status_code == 200
    assert data["ok"] is False
    assert [(r["kind"], r["outcome"]) for r in data["results"]] == [
        ("boiler", "pushed"),
        ("timer", "failed"),
    ]


def test_push_now_needs_a_login_when_login_is_required() -> None:
    site = Site(login=True)
    assert site.post("/api/push", {}).status_code == 401


def test_items_tell_which_days_share_one_event_and_describe_timers(site: Site) -> None:
    create_morning(site, days=[2, 4])
    site.post(
        "/api/schedules/timer",
        {
            "days": [3],
            "relays": [4, 5],
            "start_astro": {"astro": "sunset", "offset": -15},
            "duration_s": 3600,
        },
    )
    site.world.sync.sync_once()
    items = site.client.get("/api/schedules").get_json()["items"]
    boiler = [i for i in items if i["kind"] == "boiler"]
    assert {i["series"] for i in boiler} == {boiler[0]["series"]}  # one event, two days
    assert all(i["series_days"] == [2, 4] for i in boiler)
    timer = next(i for i in items if i["kind"] == "timer")
    assert timer["relays"] == [4, 5]
    assert timer["start_astro"] == {"astro": "sunset", "offset": -15}
    assert (timer["end"], timer["end_astro"], timer["duration_s"]) == (None, None, 3600)


# --- the page ---------------------------------------------------------------------------------

PAGE = UI_FILE.read_text(encoding="utf-8")
PAGE_BUDGET = 60 * 1024
PAGE_BUDGET_GZIP = 20 * 1024


def test_the_page_is_served_with_a_policy_that_allows_only_itself(site: Site) -> None:
    answer = site.client.get("/")
    assert answer.status_code == 200
    assert answer.mimetype == "text/html"
    policy = answer.headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy
    assert "http://" not in policy and "https://" not in policy
    assert answer.headers["Cache-Control"] == "no-cache"


def test_the_page_is_gzipped_for_browsers_that_accept_it(site: Site) -> None:
    plain = site.client.get("/", headers={"Accept-Encoding": "identity"})
    packed = site.client.get("/", headers={"Accept-Encoding": "gzip, deflate"})
    assert "Content-Encoding" not in plain.headers
    assert packed.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(packed.get_data()) == plain.get_data()
    assert packed.headers["Vary"] == "Accept-Encoding"


def test_an_unchanged_page_is_answered_with_304(site: Site) -> None:
    tag = site.client.get("/").headers["ETag"]
    again = site.client.get("/", headers={"If-None-Match": tag})
    assert again.status_code == 304
    assert again.get_data() == b""


def test_the_page_fits_the_size_budget_plain_and_gzipped(site: Site) -> None:
    plain = site.client.get("/", headers={"Accept-Encoding": "identity"}).get_data()
    packed = site.client.get("/", headers={"Accept-Encoding": "gzip"}).get_data()
    assert len(plain) <= PAGE_BUDGET, len(plain)
    assert len(packed) <= PAGE_BUDGET_GZIP, len(packed)


def test_the_page_loads_nothing_from_outside_and_uses_no_dangerous_calls() -> None:
    assert not re.findall(r"https?://", PAGE)
    assert not re.findall(r"<(script|link|img|iframe)[^>]+\b(src|href)=", PAGE)
    for forbidden in ("eval(", "document.write", "new Function", "importScripts", "localStorage"):
        assert forbidden not in PAGE, forbidden
    assert "UI mock" not in PAGE and "Mock controls" not in PAGE


def test_every_element_the_script_looks_up_exists_in_the_page() -> None:
    ids = set(re.findall(r'\bid="([^"]+)"', PAGE))
    used = set(re.findall(r"\$\('#([A-Za-z0-9_]+)'\)", PAGE))
    assert used - ids == set()
    used |= set(re.findall(r"\$\$\('#([A-Za-z0-9_]+)\b", PAGE))
    assert used - ids == set()


def test_every_api_call_of_the_page_is_a_route_that_exists(site: Site) -> None:
    calls = set(re.findall(r"api\('(GET|POST|PUT|DELETE)', '(/api/[^']*)'", PAGE))
    assert len(calls) >= 12
    adapter = site.app.url_map.bind("localhost")
    for method, url in calls:
        # the page appends ids to some addresses; try the address as written and with a stand-in
        candidates = [url, url + "1", url + "x", url + "x/x"]
        assert any(_routes(adapter, path, method) for path in candidates), (method, url)


def _routes(adapter: Any, path: str, method: str) -> bool:
    try:
        adapter.match(path, method=method)
    except HTTPException:
        return False
    return True


def test_text_that_comes_from_the_calendar_is_always_escaped_in_the_page() -> None:
    # event titles reach the browser only as ids and numbers, never as raw html
    for interpolation in re.findall(r"\$\{([^}]*(?:\.id|\.title|\.summary)[^}]*)\}", PAGE):
        assert "esc(" in interpolation, interpolation
