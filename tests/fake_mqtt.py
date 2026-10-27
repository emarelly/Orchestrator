"""Fakes for the MQTT tests: a link without a broker, and a stand-in for the paho client module."""

from collections.abc import Callable
from typing import Any, ClassVar


class FakeMqttLink:
    """Records everything the bridge does and lets a test play the broker."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.retained: dict[str, str] = {}
        self.subscribed: list[str] = []
        self.started = False
        self.stopped = False
        self.accepting = True  # False: publish fails, as when the connection is gone
        self._on_connect: Callable[[], None] = lambda: None
        self._on_disconnect: Callable[[], None] = lambda: None
        self._on_message: Callable[[str, bytes, bool], None] = lambda t, p, r: None

    def start(
        self,
        on_connect: Callable[[], None],
        on_disconnect: Callable[[], None],
        on_message: Callable[[str, bytes, bool], None],
    ) -> None:
        self.started = True
        self._on_connect, self._on_disconnect, self._on_message = (
            on_connect,
            on_disconnect,
            on_message,
        )

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> bool:
        if not self.accepting:
            return False
        self.published.append((topic, payload, retain))
        if retain:
            self.retained[topic] = payload
        return True

    def subscribe(self, topic: str) -> bool:
        self.subscribed.append(topic)
        return True

    def stop(self) -> None:
        self.stopped = True

    # --- playing the broker ----------------------------------------------------------------------

    def connect(self) -> None:
        self._on_connect()

    def drop(self) -> None:
        self._on_disconnect()

    def receive(self, topic: str, payload: bytes | str, *, retained: bool = False) -> None:
        data = payload.encode() if isinstance(payload, str) else payload
        self._on_message(topic, data, retained)

    def messages_on(self, topic: str) -> list[str]:
        return [payload for t, payload, _ in self.published if t == topic]


class FakePahoClient:
    """Stands in for ``paho.mqtt.client.Client`` and records how it was configured."""

    instances: ClassVar[list["FakePahoClient"]] = []

    def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
        self.client_id = client_id
        self.clean_session = clean_session
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.on_connect: Callable[..., None] | None = None
        self.on_disconnect: Callable[..., None] | None = None
        self.on_message: Callable[..., None] | None = None
        self.publish_rc = 0
        FakePahoClient.instances.append(self)

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args_of(self, name: str) -> tuple[Any, ...]:
        return next(args for n, args in self.calls if n == name)

    def username_pw_set(self, username: str, password: str | None) -> None:
        self._record("username_pw_set", username, password)

    def tls_set(self) -> None:
        self._record("tls_set")

    def will_set(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self._record("will_set", topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self._record("reconnect_delay_set", min_delay, max_delay)

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        self._record("connect_async", host, port, keepalive)

    def loop_start(self) -> None:
        self._record("loop_start")

    def loop_stop(self) -> None:
        self._record("loop_stop")

    def disconnect(self) -> None:
        self._record("disconnect")

    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> "FakePahoClient":
        self._record("publish", topic, payload, qos, retain)
        return self

    @property
    def rc(self) -> int:
        return self.publish_rc

    def subscribe(self, topic: str, qos: int) -> tuple[int, int]:
        self._record("subscribe", topic, qos)
        return (0, 1)


class FakePahoMessage:
    def __init__(self, topic: str, payload: bytes, retain: bool) -> None:
        self.topic, self.payload, self.retain = topic, payload, retain
