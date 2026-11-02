"""MQTT bridge for Home Assistant: discovery, state, commands, reconnects (FR-MQTT-1..6)."""

import json
import logging
import threading
import time
import types
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest

from orchestrator.config import MqttConfig
from orchestrator.mqtt_bridge import (
    MANUAL_RELAYS,
    MAX_PAYLOAD_BYTES,
    QUEUE_SIZE,
    CommandError,
    MqttBridge,
    PahoLink,
    build_link,
    build_state,
    discovery_messages,
    parse_manual,
    parse_relay,
    topics_for,
)
from orchestrator.relays import RelayNames
from tests.fake_mqtt import FakeMqttLink, FakePahoClient, FakePahoMessage
from tests.test_manual_commands import Rig

CONFIG = MqttConfig(enabled=True, host="broker.local", state_interval_s=5)


class Site:
    def __init__(
        self,
        *,
        poll: bool = True,
        config: MqttConfig = CONFIG,
        diagnostics: Callable[[], dict[str, Any]] = lambda: {},
    ) -> None:
        self.rig = Rig(poll_first=poll)
        self.link = FakeMqttLink()
        self.config = config
        self.bridge = MqttBridge(
            self.link,
            config,
            manual=self.rig.commands,
            cache=self.rig.cache,
            clock=self.rig.clock,
            relay_names=RelayNames(),
            version="9.9.9",
            diagnostics=diagnostics,
        )
        self.topics = topics_for(config)

    def connect(self) -> None:
        self.bridge.begin()
        self.link.connect()
        self.drain()

    def drain(self) -> None:
        while self.bridge.step():
            pass

    def command(self, topic: str, payload: bytes | str, *, retained: bool = False) -> None:
        self.link.receive(topic, payload, retained=retained)
        self.drain()

    def state(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.link.retained[self.topics.state])
        return loaded

    def last_command(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.link.retained[self.topics.last_command])
        return loaded

    def posts(self) -> int:
        return len(self.rig.posts())


@pytest.fixture
def site() -> Iterator[Site]:
    made = Site()
    made.connect()
    yield made
    made.rig.session.close()


# --- payload parsing -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"on", ("on", None)),
        (b" OFF \n", ("off", None)),
        (b"auto", ("auto", None)),
        (b'{"action": "on", "hours": 2}', ("on", 2.0)),
        (b'{"action": "Off", "hours": 0.5}', ("off", 0.5)),
        (b'{"action": "auto"}', ("auto", None)),
    ],
)
def test_manual_payloads_that_are_accepted(
    payload: bytes, expected: tuple[str, float | None]
) -> None:
    assert parse_manual(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"boil",
        b"on now",
        b"{",
        b"[1]",
        b'"on"',
        b'{"action": "on", "extra": 1}',
        b'{"action": 5}',
        b'{"action": "on", "hours": "2"}',
        b'{"action": "on", "hours": true}',
        b'{"action": "auto", "hours": 2}',
        b"\xff\xfe",
        b"x" * (MAX_PAYLOAD_BYTES + 1),
    ],
)
def test_manual_payloads_that_are_refused(payload: bytes) -> None:
    with pytest.raises(CommandError):
        parse_manual(payload)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"ON", ("ON", None)),
        (b"off", ("OFF", None)),
        (b'{"state": "on", "seconds": 300}', ("ON", 300)),
        (b'{"state": "OFF"}', ("OFF", None)),
        (b'{"state": "OFF", "seconds": 30}', ("OFF", 30)),
        (b'{"state": "ON", "seconds": 0}', ("ON", 0)),
    ],
)
def test_relay_payloads_that_are_accepted(payload: bytes, expected: tuple[str, int | None]) -> None:
    assert parse_relay(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        b"toggle",
        b"",
        b'{"state": "ON", "seconds": 1.5}',
        b'{"state": "ON", "seconds": "5"}',
        b'{"state": "ON", "seconds": true}',
        b'{"seconds": 30}',
        b'{"state": "ON", "x": 1}',
    ],
)
def test_relay_payloads_that_are_refused(payload: bytes) -> None:
    with pytest.raises(CommandError):
        parse_relay(payload)


# --- discovery ---------------------------------------------------------------------------------


def test_discovery_describes_every_required_entity() -> None:
    messages = discovery_messages(CONFIG, RelayNames(), "9.9.9")
    parsed = {topic: json.loads(body) for topic, body in messages}
    kinds: dict[str, int] = {}
    for topic in parsed:
        kinds[topic.split("/")[1]] = kinds.get(topic.split("/")[1], 0) + 1
    assert kinds == {"sensor": 17, "binary_sensor": 2, "button": 3, "switch": 6}
    assert len({body["unique_id"] for body in parsed.values()}) == 28


def test_every_entity_shares_the_device_and_the_availability_topic() -> None:
    topics = topics_for(CONFIG)
    for topic, body in discovery_messages(CONFIG, RelayNames(), "9.9.9"):
        data = json.loads(body)
        assert topic.startswith("homeassistant/") and topic.endswith("/config")
        assert data["availability_topic"] == topics.availability
        assert (data["payload_available"], data["payload_not_available"]) == ("online", "offline")
        assert data["device"]["identifiers"] == [topics.node]
        assert data["device"]["sw_version"] == "9.9.9"


def test_sensors_read_the_state_topic_and_cope_with_missing_values() -> None:
    topics = topics_for(CONFIG)
    by_key = {
        t.split("/")[-2]: json.loads(b) for t, b in discovery_messages(CONFIG, RelayNames(), "v")
    }
    upper = by_key["upper"]
    assert upper["state_topic"] == topics.state
    assert upper["unit_of_measurement"] == "°C" and upper["device_class"] == "temperature"
    assert "is defined else none" in upper["value_template"]
    assert by_key["heater"]["payload_on"] == "ON" and by_key["heater"]["device_class"] == "heat"
    assert by_key["connection"]["entity_category"] == "diagnostic"


def test_buttons_send_the_manual_actions_and_switches_one_relay_each() -> None:
    topics = topics_for(CONFIG)
    by_key = {
        t.split("/")[-2]: json.loads(b) for t, b in discovery_messages(CONFIG, RelayNames(), "v")
    }
    assert {k: by_key[k]["payload_press"] for k in ("manual_on", "manual_off", "manual_auto")} == {
        "manual_on": "on",
        "manual_off": "off",
        "manual_auto": "auto",
    }
    assert all(by_key[k]["command_topic"] == topics.manual for k in ("manual_on", "manual_off"))
    for number in MANUAL_RELAYS:
        switch = by_key[f"relay_{number}"]
        assert switch["command_topic"] == topics.relay(number)
        assert switch["optimistic"] is True  # the state of a relay is not read back
    assert "relay_0" not in by_key and "relay_1" not in by_key  # the boiler's own relays
    assert "GardenLights" in by_key["relay_4"]["name"]
    assert by_key["relay_6"]["name"] != by_key["relay_7"]["name"]  # duplicate names stay distinct


# --- connecting --------------------------------------------------------------------------------


def test_nothing_is_published_before_the_connection_is_up() -> None:
    made = Site()
    made.bridge.begin()
    made.drain()
    assert made.link.published == [] and made.link.started
    made.rig.session.close()


def test_after_connecting_it_subscribes_announces_and_publishes_the_state(site: Site) -> None:
    topics = site.topics
    assert set(site.link.subscribed) == {
        topics.manual,
        topics.ha_status,
        *(topics.relay(n) for n in MANUAL_RELAYS),
    }
    assert site.link.retained[topics.availability] == "online"
    assert all(retain for _, _, retain in site.link.published)  # everything announced is retained
    assert len([t for t in site.link.retained if t.startswith("homeassistant/")]) == 28
    assert site.state()["connection"] == "up"


def test_a_reconnection_announces_everything_again_and_a_drop_stops_publishing(site: Site) -> None:
    site.link.drop()
    before = len(site.link.published)
    site.rig.pi.status.update(CurrentShowers=1.0)
    site.rig.poller.poll_once()
    site.drain()
    assert len(site.link.published) == before and not site.bridge.connected
    site.link.connect()
    site.drain()
    assert len(site.link.published) > before
    assert site.link.messages_on(site.topics.availability) == ["online", "online"]


def test_home_assistant_restarting_triggers_the_discovery_again(site: Site) -> None:
    count = len(site.link.messages_on(site.topics.discovery("sensor", "upper")))
    site.command(site.topics.ha_status, "offline")
    assert len(site.link.messages_on(site.topics.discovery("sensor", "upper"))) == count
    site.command(site.topics.ha_status, "online")
    assert len(site.link.messages_on(site.topics.discovery("sensor", "upper"))) == count + 1


# --- the state ---------------------------------------------------------------------------------


def test_the_state_has_the_boiler_values_with_the_hottest_level_on_top(site: Site) -> None:
    state = site.state()
    assert state["upper"] >= state["mid"] >= state["lower"]
    assert state["heater"] in ("ON", "OFF") and state["active"] in ("ON", "OFF")
    assert state["mode"] in ("Auto", "Manual", "Off")
    assert state["showers_new"] == round(state["showers_new"], 2)
    assert {"target", "min", "minutes_to_start", "updated"} <= set(state)
    assert state["stale"] is False


def test_an_unchanged_state_is_not_repeated_until_the_interval_has_passed(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [time.monotonic()]
    monkeypatch.setattr("orchestrator.mqtt_bridge.time.monotonic", lambda: now[0])
    site.drain()
    published = len(site.link.messages_on(site.topics.state))
    now[0] += 4
    site.bridge.step()
    assert len(site.link.messages_on(site.topics.state)) == published
    now[0] += 2  # 6 s >= state_interval_s (5)
    site.bridge.step()
    assert len(site.link.messages_on(site.topics.state)) == published + 1


def test_a_changed_state_is_published_at_once(site: Site) -> None:
    published = len(site.link.messages_on(site.topics.state))
    site.rig.pi.status.update(CurrentShowersnew=1.23)
    site.rig.poller.poll_once()
    site.bridge.step()
    assert len(site.link.messages_on(site.topics.state)) == published + 1
    assert site.state()["showers_new"] == 1.23


def test_a_reading_that_is_missing_old_or_unreachable_is_never_published_as_values() -> None:
    cold = Site(poll=False)
    cold.connect()
    assert cold.state() == {"connection": "unknown", "stale": True}
    cold.rig.session.close()
    old = Site()
    old.connect()
    old.rig.clock.advance(timedelta(minutes=10))
    state = build_state(old.rig.cache, old.rig.clock, timedelta(minutes=5))
    assert state["stale"] is True and "upper" not in state and "heater" not in state
    old.rig.session.close()


def test_a_failed_publish_is_retried_on_the_next_round(site: Site) -> None:
    site.rig.pi.status.update(CurrentShowersnew=2.5)
    site.rig.poller.poll_once()
    site.link.accepting = False
    site.bridge.step()
    site.link.accepting = True
    site.bridge.step()
    assert site.state()["showers_new"] == 2.5


# --- commands ----------------------------------------------------------------------------------


def test_commands_are_only_queued_by_the_network_thread(site: Site) -> None:
    site.link.receive(site.topics.manual, "off")
    assert site.posts() == 0  # nothing ran yet: the slow work is for the worker thread
    site.drain()
    assert site.posts() == 1


@pytest.mark.parametrize("action", ["on", "off", "auto"])
def test_the_manual_buttons_run_the_same_commands_as_the_ui(site: Site, action: str) -> None:
    site.command(site.topics.manual, action)
    assert site.posts() == 1
    result = site.last_command()
    assert result["ok"] is True and result["command"] == f"manual {action}"
    assert result["id"] and result["at"].startswith("2026-10-24T16:41")
    assert site.state()["mode"] in ("Auto", "Manual", "Off")


@pytest.mark.parametrize("hours", [0, 0.1, 2, 12.5, 100, -1])
def test_a_duration_is_refused_because_the_boiler_pi_has_fixed_durations(
    site: Site, hours: float
) -> None:
    site.command(site.topics.manual, json.dumps({"action": "on", "hours": hours}))
    assert site.posts() == 0
    assert site.last_command()["ok"] is False and "duration" in site.last_command()["message"]


@pytest.mark.parametrize("payload", ["boil", "{", '{"action": "on", "x": 1}', b"\xff", "x" * 300])
def test_unusable_payloads_are_reported_and_change_nothing(
    site: Site, payload: str | bytes
) -> None:
    site.command(site.topics.manual, payload)
    assert site.posts() == 0
    assert site.last_command()["ok"] is False
    assert site.last_command()["message"].startswith("invalid command")


def test_a_retained_command_is_never_run(site: Site) -> None:
    before = site.link.retained.get(site.topics.last_command)
    site.command(site.topics.manual, "on", retained=True)
    site.command(site.topics.relay(4), "ON", retained=True)
    assert site.posts() == 0
    assert site.link.retained.get(site.topics.last_command) == before


def test_a_refusal_by_the_shared_rules_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    cold = Site(poll=False)
    cold.connect()
    cold.command(cold.topics.relay(4), '{"state": "ON", "seconds": 5}')  # too short, and not 0
    assert cold.posts() == 0
    assert cold.last_command()["ok"] is False and "duration" in cold.last_command()["message"]
    cold.rig.session.close()


def test_a_relay_switch_runs_for_the_default_time_or_the_given_one(site: Site) -> None:
    site.command(site.topics.relay(4), "ON")
    assert (
        site.last_command()["ok"] is True
        and "Relay 4 on for 600 s" in site.last_command()["message"]
    )
    site.command(site.topics.relay(4), '{"state": "ON", "seconds": 120}')
    assert "Relay 4 on for 120 s" in site.last_command()["message"]
    site.command(site.topics.relay(4), "OFF")
    assert site.last_command()["command"] == "relay 4 off" and site.last_command()["ok"] is True
    assert "without a time limit" in site.last_command()["message"]
    assert site.posts() == 3


@pytest.mark.parametrize("seconds", [10, 1, 86401])
def test_relay_durations_outside_the_rules_are_refused(site: Site, seconds: int) -> None:
    site.command(site.topics.relay(3), json.dumps({"state": "ON", "seconds": seconds}))
    assert site.posts() == 0 and site.last_command()["ok"] is False


def test_the_boilers_own_relays_and_other_topics_are_ignored(site: Site) -> None:
    base = site.topics.base
    for topic in (
        f"{base}/cmd/relay/0",
        f"{base}/cmd/relay/1",
        f"{base}/cmd/relay/8",
        f"{base}/other",
    ):
        site.command(topic, "ON")
    assert site.posts() == 0


def test_a_burst_of_commands_cannot_grow_the_queue_without_limit(site: Site) -> None:
    for _ in range(QUEUE_SIZE * 3):
        site.link.receive(site.topics.manual, "auto")
    site.drain()
    assert 1 <= site.posts() <= QUEUE_SIZE


def test_commands_are_audited_as_coming_from_mqtt(
    site: Site, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        site.command(site.topics.manual, "auto")
    assert "channel=mqtt" in caplog.text and "who=mqtt" in caplog.text


# --- the thread and the shutdown -------------------------------------------------------------


def wait_until(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_the_thread_works_until_stopped_and_then_says_offline() -> None:
    made = Site()
    stop = threading.Event()
    thread = made.bridge.start(stop)
    assert wait_until(lambda: made.link.started)
    made.link.connect()
    assert wait_until(lambda: made.topics.state in made.link.retained)
    made.link.receive(made.topics.manual, "auto")
    assert wait_until(lambda: made.topics.last_command in made.link.retained)
    assert made.bridge.last_cycle is not None
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert made.link.stopped and made.link.retained[made.topics.availability] == "offline"
    made.rig.session.close()


def test_a_crashing_job_does_not_end_the_loop(
    site: Site, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(**_: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(site.rig.commands, "auto", boom)
    with caplog.at_level(logging.ERROR):
        site.command(site.topics.manual, "auto")
    assert "event=mqtt_worker_error" in caplog.text
    monkeypatch.undo()
    site.command(site.topics.manual, "off")
    assert site.posts() == 1


def test_a_shutdown_before_the_connection_publishes_nothing() -> None:
    made = Site()
    made.bridge.begin()
    made.bridge.shutdown()
    assert made.link.published == [] and made.link.stopped
    made.rig.session.close()


# --- the real client, with a stand-in for the paho module ----------------------------------------


@pytest.fixture
def paho(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakePahoClient]]:
    FakePahoClient.instances.clear()
    module = types.SimpleNamespace(Client=FakePahoClient)
    real = __import__("importlib").import_module
    monkeypatch.setattr(
        "orchestrator.mqtt_bridge.importlib.import_module",
        lambda name, *a, **k: module if name == "paho.mqtt.client" else real(name, *a, **k),
    )
    yield FakePahoClient


def started_link(config: MqttConfig, password: str | None = None) -> tuple[PahoLink, FakeMqttLink]:
    link = PahoLink(config, password, topics_for(config).availability)
    probe = FakeMqttLink()
    probe.started = False
    seen: dict[str, Any] = {}
    link.start(
        lambda: seen.setdefault("connected", []).append(True),
        lambda: seen.setdefault("disconnected", []).append(True),
        lambda t, p, r: seen.setdefault("messages", []).append((t, p, r)),
    )
    probe.published.append(("seen", json.dumps(seen), False))
    return link, probe


def test_the_client_is_set_up_with_a_last_will_and_automatic_reconnects(
    paho: type[FakePahoClient],
) -> None:
    config = MqttConfig(
        enabled=True,
        host="broker.local",
        port=1884,
        username="ha",
        keepalive_s=30,
        client_id="orch-1",
        base_topic="home/boiler",
    )
    started_link(config, "s3cret")
    client = paho.instances[0]
    assert (client.client_id, client.clean_session) == ("orch-1", True)
    assert client.args_of("username_pw_set") == ("ha", "s3cret")
    assert client.args_of("will_set") == ("home/boiler/availability", "offline", 1, True)
    assert client.args_of("reconnect_delay_set") == (1, 60)
    assert client.args_of("connect_async") == ("broker.local", 1884, 30)
    names = client.names()
    assert names.index("will_set") < names.index("connect_async") < names.index("loop_start")
    assert "tls_set" not in names


def test_tls_is_used_only_when_asked_for_and_no_user_means_no_login(
    paho: type[FakePahoClient],
) -> None:
    started_link(MqttConfig(enabled=True, host="b", tls=True))
    names = paho.instances[0].names()
    assert "tls_set" in names and "username_pw_set" not in names


def test_the_callbacks_are_mapped_and_a_refused_connection_is_not_a_connection(
    paho: type[FakePahoClient], caplog: pytest.LogCaptureFixture
) -> None:
    events: list[str] = []
    messages: list[tuple[str, bytes, bool]] = []
    link = PahoLink(CONFIG, None, "t/availability")
    link.start(
        lambda: events.append("up"),
        lambda: events.append("down"),
        lambda t, p, r: messages.append((t, p, r)),
    )
    client = paho.instances[0]
    assert client.on_connect and client.on_disconnect and client.on_message
    with caplog.at_level(logging.WARNING):
        client.on_connect(client, None, {}, 5)  # "not authorised"
        client.on_connect(client, None, {}, 0)
        client.on_disconnect(client, None, 1)
    client.on_message(client, None, FakePahoMessage("a/b", b"payload", True))
    client.on_message(client, None, FakePahoMessage("a/c", b"x", False))
    assert events == ["up", "down"]
    assert messages == [("a/b", b"payload", True), ("a/c", b"x", False)]
    assert "event=mqtt_connect_refused code=5" in caplog.text


def test_publishing_uses_quality_1_and_reports_failure(paho: type[FakePahoClient]) -> None:
    link = PahoLink(CONFIG, None, "t/availability")
    assert link.publish("a", "b") is False and link.subscribe("a") is False  # before start
    link.start(lambda: None, lambda: None, lambda t, p, r: None)
    client = paho.instances[0]
    assert link.publish("a/b", "x", retain=True) is True
    assert client.args_of("publish") == ("a/b", "x", 1, True)
    client.publish_rc = 4  # not connected
    assert link.publish("a/b", "y") is False
    assert link.subscribe("a/#") is True and client.args_of("subscribe") == ("a/#", 1)


def test_stopping_ends_the_network_loop_then_disconnects_and_is_repeatable(
    paho: type[FakePahoClient],
) -> None:
    link = PahoLink(CONFIG, None, "t/availability")
    link.start(lambda: None, lambda: None, lambda t, p, r: None)
    link.stop()
    link.stop()
    names = paho.instances[0].names()
    assert names[-2:] == ["loop_stop", "disconnect"] and names.count("disconnect") == 1


def test_the_password_never_reaches_the_log(
    paho: type[FakePahoClient], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        started_link(MqttConfig(enabled=True, host="b", username="ha"), "pw-SENTINEL-9")
        client = paho.instances[0]
        assert client.on_connect is not None
        client.on_connect(client, None, {}, 5)
    assert "pw-SENTINEL-9" not in caplog.text


def test_build_link_uses_the_availability_topic_as_the_last_will(
    paho: type[FakePahoClient],
) -> None:
    link = build_link(MqttConfig(enabled=True, host="b", base_topic="x/y"), None)
    link.start(lambda: None, lambda: None, lambda t, p, r: None)
    assert paho.instances[0].args_of("will_set")[0] == "x/y/availability"


# --- the diagnostic entities (OPS-9) -------------------------------------------------------------


def test_the_orchestrators_own_health_has_its_own_entities() -> None:
    by_key = {
        t.split("/")[-2]: json.loads(b) for t, b in discovery_messages(CONFIG, RelayNames(), "v")
    }
    keys = [
        "version",
        "uptime_min",
        "memory_mb",
        "cpu_temp_c",
        "last_calendar_sync",
        "pending_pushes",
        "failed_components",
    ]
    for key in keys:
        assert by_key[key]["entity_category"] == "diagnostic", key
        assert by_key[key]["state_topic"] == topics_for(CONFIG).state
    assert by_key["last_calendar_sync"]["device_class"] == "timestamp"


def test_the_health_values_are_published_even_when_the_boiler_reading_is_not() -> None:
    values = {"version": "9.9.9", "uptime_min": 12, "failed_components": 0}
    cold = Site(poll=False, diagnostics=lambda: values)
    cold.connect()
    state = cold.state()
    assert state["connection"] == "unknown" and "upper" not in state  # no boiler values
    assert state["version"] == "9.9.9" and state["uptime_min"] == 12  # but the service's own
    cold.rig.session.close()
