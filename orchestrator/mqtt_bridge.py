"""MQTT bridge for Home Assistant (FR-MQTT-1..6).

What it publishes (``base`` is ``[mqtt] base_topic``):

* ``base/availability``  ``online`` / ``offline`` (retained; ``offline`` is also the last will, so
  Home Assistant shows the Orchestrator as offline when it dies, OPS-9);
* ``base/state``         one JSON object with the boiler values (retained), on each change and at
  least every ``state_interval_s``. A missing, old or unreachable reading is **not** published as
  values (the entities become "unknown"): a heater state is never guessed;
* ``base/last_command``  the outcome of the last command (retained);
* Home Assistant discovery messages under ``<discovery_prefix>/...`` (retained), so that the
  entities appear without YAML.

What it accepts, handled by the same objects as the UI (``source="mqtt"``):

* ``base/cmd/manual``    ``on`` / ``off`` / ``auto`` or ``{"action": "on", "hours": 2}``;
* ``base/cmd/relay/<n>`` ``ON`` / ``OFF`` or ``{"state": "ON", "seconds": 300}`` (relays 2..7).

Retained command messages are ignored (a broker restart must never press a button again), command
payloads are small and strictly checked, and the (slow) work of a command never runs on the MQTT
network thread.
"""

import importlib
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import Any, Protocol

from orchestrator.clock import Clock
from orchestrator.config import MqttConfig
from orchestrator.logging_setup import kv
from orchestrator.manual_commands import CommandResult, ManualCommands
from orchestrator.relays import RelayNames
from orchestrator.status_cache import ConnectionState, StatusCache
from orchestrator.temperatures import levels_of

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 256
QUEUE_SIZE = 16
MANUAL_RELAYS = range(2, 8)
ONLINE, OFFLINE = "online", "offline"


class MqttLink(Protocol):
    """The part of an MQTT client that the bridge uses (a fake in the tests)."""

    def start(
        self,
        on_connect: Callable[[], None],
        on_disconnect: Callable[[], None],
        on_message: Callable[[str, bytes, bool], None],
    ) -> None: ...

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> bool: ...

    def subscribe(self, topic: str) -> bool: ...

    def stop(self) -> None: ...


class PahoLink:
    """The real client (paho-mqtt 1.6; imported only when MQTT is used)."""

    def __init__(self, config: MqttConfig, password: str | None, will_topic: str) -> None:
        self._config = config
        self._password = password
        self._will_topic = will_topic
        self._client: Any = None

    def start(
        self,
        on_connect: Callable[[], None],
        on_disconnect: Callable[[], None],
        on_message: Callable[[str, bytes, bool], None],
    ) -> None:
        mqtt = importlib.import_module("paho.mqtt.client")
        config = self._config
        client = mqtt.Client(client_id=config.client_id, clean_session=True)
        if config.username:
            client.username_pw_set(config.username, self._password)
        if config.tls:
            client.tls_set()  # system certificate authorities, host name is checked
        client.will_set(self._will_topic, OFFLINE, qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        def connected(_client: Any, _userdata: Any, _flags: Any, rc: int) -> None:
            if rc == 0:
                on_connect()
            else:
                logger.warning(kv("mqtt_connect_refused", code=rc))

        def disconnected(_client: Any, _userdata: Any, rc: int) -> None:
            logger.warning(kv("mqtt_disconnected", code=rc))
            on_disconnect()

        def message(_client: Any, _userdata: Any, msg: Any) -> None:
            on_message(str(msg.topic), bytes(msg.payload), bool(msg.retain))

        client.on_connect = connected
        client.on_disconnect = disconnected
        client.on_message = message
        client.connect_async(config.host, config.port, keepalive=config.keepalive_s)
        client.loop_start()  # its own thread; it reconnects by itself
        self._client = client

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> bool:
        if self._client is None:
            return False
        info = self._client.publish(topic, payload, qos=1, retain=retain)
        return bool(info.rc == 0)

    def subscribe(self, topic: str) -> bool:
        if self._client is None:
            return False
        result, _ = self._client.subscribe(topic, qos=1)
        return bool(result == 0)

    def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.loop_stop()
            client.disconnect()


# --- topics and the messages that Home Assistant discovers -------------------------------------


@dataclass(frozen=True, slots=True)
class Topics:
    base: str
    prefix: str
    node: str

    @property
    def availability(self) -> str:
        return f"{self.base}/availability"

    @property
    def state(self) -> str:
        return f"{self.base}/state"

    @property
    def last_command(self) -> str:
        return f"{self.base}/last_command"

    @property
    def manual(self) -> str:
        return f"{self.base}/cmd/manual"

    def relay(self, number: int) -> str:
        return f"{self.base}/cmd/relay/{number}"

    @property
    def ha_status(self) -> str:
        return f"{self.prefix}/status"  # Home Assistant says "online" here when it (re)starts

    def discovery(self, component: str, key: str) -> str:
        return f"{self.prefix}/{component}/{self.node}/{key}/config"


def topics_for(config: MqttConfig) -> Topics:
    return Topics(config.base_topic, config.discovery_prefix, config.client_id.replace("-", "_"))


def discovery_messages(
    config: MqttConfig, relay_names: RelayNames, version: str
) -> list[tuple[str, str]]:
    """Every discovery message as (topic, json text), in a stable order."""
    topics = topics_for(config)
    device = {
        "identifiers": [topics.node],
        "name": "Home Control Orchestrator",
        "manufacturer": "MyHomeControl",
        "model": "Boiler Orchestrator",
        "sw_version": version,
    }

    def entity(component: str, key: str, name: str, **fields: Any) -> tuple[str, str]:
        body = {
            "name": name,
            "unique_id": f"{topics.node}_{key}",
            "object_id": f"{topics.node}_{key}",
            "availability_topic": topics.availability,
            "payload_available": ONLINE,
            "payload_not_available": OFFLINE,
            "device": device,
            **fields,
        }
        return topics.discovery(component, key), json.dumps(body, sort_keys=True)

    def value(key: str) -> dict[str, str]:
        template = f"{{{{ value_json.{key} if value_json.{key} is defined else none }}}}"
        return {"state_topic": topics.state, "value_template": template}

    def sensor(key: str, name: str, **fields: Any) -> tuple[str, str]:
        return entity("sensor", key, name, **value(key), **fields)

    celsius = {"unit_of_measurement": "°C", "device_class": "temperature"}
    messages = [
        sensor("upper", "Boiler upper temperature", state_class="measurement", **celsius),
        sensor("mid", "Boiler middle temperature", state_class="measurement", **celsius),
        sensor("lower", "Boiler lower temperature", state_class="measurement", **celsius),
        sensor(
            "showers_new",
            "Showers available (thermodynamic)",
            unit_of_measurement="showers",
            state_class="measurement",
            icon="mdi:shower-head",
        ),
        sensor(
            "showers_old",
            "Showers available (linear)",
            unit_of_measurement="showers",
            state_class="measurement",
            icon="mdi:shower-head",
        ),
        sensor("target", "Boiler target", unit_of_measurement="showers", icon="mdi:target"),
        sensor(
            "min", "Boiler minimum", unit_of_measurement="showers", icon="mdi:arrow-collapse-down"
        ),
        sensor(
            "minutes_to_start",
            "Minutes to next heating",
            unit_of_measurement="min",
            device_class="duration",
        ),
        sensor("mode", "Boiler mode", icon="mdi:thermostat"),
        sensor("connection", "Boiler Pi connection", entity_category="diagnostic"),
        # the Orchestrator's own health (OPS-9); these keep working when the boiler Pi does not
        sensor("version", "Orchestrator version", entity_category="diagnostic"),
        sensor(
            "uptime_min",
            "Orchestrator uptime",
            entity_category="diagnostic",
            unit_of_measurement="min",
            device_class="duration",
        ),
        sensor(
            "memory_mb",
            "Orchestrator memory",
            entity_category="diagnostic",
            unit_of_measurement="MB",
            device_class="data_size",
            state_class="measurement",
        ),
        sensor(
            "cpu_temp_c",
            "Orchestrator CPU temperature",
            entity_category="diagnostic",
            state_class="measurement",
            **celsius,
        ),
        sensor(
            "last_calendar_sync",
            "Last calendar read",
            entity_category="diagnostic",
            device_class="timestamp",
        ),
        sensor(
            "pending_pushes",
            "Schedule files waiting",
            entity_category="diagnostic",
            state_class="measurement",
        ),
        sensor(
            "failed_components",
            "Failed components",
            entity_category="diagnostic",
            state_class="measurement",
        ),
    ]
    for key, name, device_class in (
        ("heater", "Boiler heater", "heat"),
        ("active", "Boiler service active", "running"),
    ):
        messages.append(
            entity(
                "binary_sensor",
                key,
                name,
                **value(key),
                payload_on="ON",
                payload_off="OFF",
                device_class=device_class,
            )
        )
    for key, name, payload in (
        ("manual_on", "Boiler manual ON", "on"),
        ("manual_off", "Boiler manual OFF", "off"),
        ("manual_auto", "Boiler back to schedule", "auto"),
    ):
        messages.append(
            entity("button", key, name, command_topic=topics.manual, payload_press=payload)
        )
    for number in MANUAL_RELAYS:
        label = relay_names.name_of(number)
        messages.append(
            entity(
                "switch",
                f"relay_{number}",
                f"Relay {number}" + (f" ({label})" if label else ""),
                command_topic=topics.relay(number),
                payload_on="ON",
                payload_off="OFF",
                optimistic=True,  # the relay state is not read back, so Home Assistant assumes it
                icon="mdi:power-socket-eu",
            )
        )
    return messages


# --- the state -------------------------------------------------------------------------------


def build_state(cache: StatusCache, clock: Clock, stale_after: timedelta) -> dict[str, Any]:
    """The values to publish; only a trustworthy reading produces values."""
    snapshot = cache.snapshot()
    stale = snapshot.is_stale(clock.now(), stale_after)
    state: dict[str, Any] = {"connection": snapshot.state.value, "stale": stale}
    status = snapshot.status
    if status is None or stale or snapshot.state in (ConnectionState.DOWN, ConnectionState.UNKNOWN):
        return state
    levels = levels_of(status)
    state.update(
        mode=status.mode,
        heater="ON" if status.heater_on else "OFF",
        active="ON" if status.active else "OFF",
        upper=round(levels.upper, 1),
        mid=round(levels.mid, 1),
        lower=round(levels.lower, 1),
        showers_new=round(status.showers_thermo, 2),
        showers_old=round(status.showers_linear, 2),
        target=round(status.target, 1),
        min=round(status.minimum, 1),
        minutes_to_start=status.minutes_to_start,
        updated=status.date.isoformat(timespec="seconds"),
    )
    return state


# --- commands from the broker ----------------------------------------------------------------


class CommandError(ValueError):
    """The payload is not acceptable; the text goes to ``last_command``."""


def _decode(payload: bytes) -> str:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise CommandError("the payload is too long")
    try:
        return payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise CommandError("the payload is not text") from None


def _json_object(text: str, allowed: set[str]) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        raise CommandError("the payload is not valid JSON") from None
    if not isinstance(data, dict) or set(data) - allowed:
        raise CommandError("the payload must be an object with only " + ", ".join(sorted(allowed)))
    return data


def parse_manual(payload: bytes) -> tuple[str, float | None]:
    """``(action, hours)`` of a boiler command; hours is ``None`` for the default."""
    text = _decode(payload)
    if text.startswith("{"):
        data = _json_object(text, {"action", "hours"})
        action, hours = data.get("action"), data.get("hours")
    else:
        action, hours = text, None
    if not isinstance(action, str) or action.lower() not in ("on", "off", "auto"):
        raise CommandError("the action must be on, off or auto")
    if hours is not None and (isinstance(hours, bool) or not isinstance(hours, int | float)):
        raise CommandError("hours must be a number")
    if hours is not None and action.lower() == "auto":
        raise CommandError("auto takes no duration")
    return action.lower(), None if hours is None else float(hours)


def parse_relay(payload: bytes) -> tuple[str, int | None]:
    """``(state, seconds)`` of a relay command; seconds is ``None`` for the default."""
    text = _decode(payload)
    if text.startswith("{"):
        data = _json_object(text, {"state", "seconds"})
        state, seconds = data.get("state"), data.get("seconds")
    else:
        state, seconds = text, None
    if not isinstance(state, str) or state.upper() not in ("ON", "OFF"):
        raise CommandError("the state must be ON or OFF")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, int)):
        raise CommandError("seconds must be a whole number")
    if seconds is not None and state.upper() == "OFF":
        raise CommandError("OFF takes no duration")
    return state.upper(), seconds


Job = Callable[[], None]


class MqttBridge:
    """Publishes the state and runs commands on one worker thread, not on the network thread."""

    def __init__(
        self,
        link: MqttLink,
        config: MqttConfig,
        *,
        manual: ManualCommands,
        cache: StatusCache,
        clock: Clock,
        relay_names: RelayNames,
        version: str,
        stale_after: timedelta = timedelta(minutes=5),
        diagnostics: Callable[[], dict[str, Any]] = lambda: {},
    ) -> None:
        self._diagnostics = diagnostics
        self._link = link
        self._config = config
        self._manual = manual
        self._cache = cache
        self._clock = clock
        self._names = relay_names
        self._version = version
        self._stale_after = stale_after
        self.topics = topics_for(config)
        self._jobs: queue.Queue[Job] = queue.Queue(maxsize=QUEUE_SIZE)
        self._connected = threading.Event()
        self._last_state: str | None = None
        self._last_published = 0.0
        self.last_cycle: float | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # --- from the network thread: only record and queue ----------------------------------------

    def _on_connect(self) -> None:
        self._connected.set()
        logger.info(kv("mqtt_connected", host=self._config.host))
        self._enqueue(self._announce)

    def _on_disconnect(self) -> None:
        self._connected.clear()

    def _on_message(self, topic: str, payload: bytes, retained: bool) -> None:
        topics = self.topics
        if topic == topics.ha_status:
            if payload.strip() == ONLINE.encode():
                self._enqueue(self._announce)  # Home Assistant restarted: tell it everything again
            return
        if retained:
            logger.warning(kv("mqtt_retained_command_ignored", topic=topic))
            return
        if topic == topics.manual:
            self._enqueue(lambda: self._manual_command(payload))
            return
        for number in MANUAL_RELAYS:
            if topic == topics.relay(number):
                self._enqueue(partial(self._relay_command, number, payload))
                return
        logger.warning(kv("mqtt_unknown_topic", topic=topic))

    def _enqueue(self, job: Job) -> None:
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            logger.warning(kv("mqtt_job_dropped"))

    # --- on the worker thread --------------------------------------------------------------------

    def _announce(self) -> None:
        """After every (re)connection: availability, discovery, subscriptions, state."""
        topics = self.topics
        for topic in (topics.manual, *(topics.relay(n) for n in MANUAL_RELAYS), topics.ha_status):
            self._link.subscribe(topic)
        for topic, body in discovery_messages(self._config, self._names, self._version):
            self._link.publish(topic, body, retain=True)
        self._link.publish(topics.availability, ONLINE, retain=True)
        self._publish_state(force=True)

    def _publish_state(self, *, force: bool = False) -> None:
        if not self.connected:
            return
        state = build_state(self._cache, self._clock, self._stale_after)
        state.update(self._diagnostics())
        text = json.dumps(state, sort_keys=True)
        due = time.monotonic() - self._last_published >= self._config.state_interval_s
        if (force or text != self._last_state or due) and self._link.publish(
            self.topics.state, text, retain=True
        ):
            self._last_state, self._last_published = text, time.monotonic()

    def _manual_command(self, payload: bytes) -> None:
        try:
            action, hours = parse_manual(payload)
        except CommandError as error:
            self._report("manual", False, f"invalid command: {error}")
            return
        who = "mqtt"
        if action == "on":
            result = self._manual.manual_on(source="mqtt", who=who, hours=hours)
        elif action == "off":
            result = self._manual.manual_off(source="mqtt", who=who, hours=hours)
        else:
            result = self._manual.auto(source="mqtt", who=who)
        self._report(f"manual {action}", result.ok, result.message, result)

    def _relay_command(self, number: int, payload: bytes) -> None:
        try:
            state, seconds = parse_relay(payload)
        except CommandError as error:
            self._report(f"relay {number}", False, f"invalid command: {error}")
            return
        if state == "ON":
            run = seconds if seconds is not None else self._config.relay_on_seconds
            result = self._manual.relay_on(number, run, source="mqtt", who="mqtt")
        else:
            result = self._manual.relay_off(number, source="mqtt", who="mqtt")
        self._report(f"relay {number} {state.lower()}", result.ok, result.message, result)

    def _report(
        self, command: str, ok: bool, message: str, result: CommandResult | None = None
    ) -> None:
        body = {
            "command": command,
            "ok": ok,
            "message": message,
            "at": self._clock.now().isoformat(timespec="seconds"),
        }
        if result is not None:
            body["id"] = result.correlation_id
        logger.info(kv("mqtt_command", command=command, ok=ok))
        if self.connected:
            self._link.publish(
                self.topics.last_command, json.dumps(body, sort_keys=True), retain=True
            )
        self._publish_state(force=True)

    # --- the thread --------------------------------------------------------------------------

    def begin(self) -> None:
        """Start the MQTT client; it connects and reconnects by itself."""
        self._link.start(self._on_connect, self._on_disconnect, self._on_message)

    def step(self, wait: float = 0.0) -> bool:
        """Run one queued job (waiting up to ``wait`` seconds) and publish the state if due.

        Returns whether a job was run.
        """
        try:
            job: Job | None = self._jobs.get(timeout=wait) if wait else self._jobs.get_nowait()
        except queue.Empty:
            job = None
        try:
            if job is not None:
                job()
            self._publish_state()
        except Exception:  # CS-11: one bad job or publish never ends the loop
            logger.exception(kv("mqtt_worker_error"))
        self.last_cycle = time.monotonic()
        return job is not None

    def run(self, stop: threading.Event) -> None:
        self.begin()
        try:
            while not stop.is_set():
                self.step(1.0)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Say ``offline`` properly (the last will only covers a crash), then disconnect."""
        if self.connected:
            self._link.publish(self.topics.availability, OFFLINE, retain=True)
        self._link.stop()
        self._connected.clear()

    def start(self, stop: threading.Event) -> threading.Thread:
        thread = threading.Thread(target=self.run, args=(stop,), name="mqtt", daemon=True)
        thread.start()
        return thread


def build_link(config: MqttConfig, password: str | None) -> MqttLink:
    """The real MQTT client for this configuration."""
    topics = topics_for(config)
    return PahoLink(config, password, topics.availability)
