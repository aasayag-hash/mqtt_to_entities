from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import paho.mqtt.client as mqtt

logger = logging.getLogger("mqtt_to_entities.mqtt_client")

MAX_BACKOFF_SECONDS = 30
PREVIEW_MAX_CHARS = 80
# How long a reconnect attempt waits for the broker's CONNACK before retrying.
CONNECT_WAIT_SECONDS = 5.0


@dataclass
class BrokerConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    name: str | None = None

    def label(self) -> str:
        return self.name or f"{self.host}:{self.port}"


@dataclass
class TopicState:
    payload: Any
    raw: str
    received_at: float = field(default_factory=time.time)
    message_count: int = 1


class BrokerConnection:
    """A single broker connection with its own topic cache and retry loop."""

    def __init__(
        self,
        broker_id: str,
        config: BrokerConfig,
        on_message: Callable[[str, str, TopicState], None] | None = None,
        on_status_change: Callable[[str, str], None] | None = None,
    ) -> None:
        self.id = broker_id
        self.config = config
        self._on_status_change_cb = on_status_change
        self._client: mqtt.Client | None = None
        self._topics: dict[str, TopicState] = {}
        self._lock = threading.Lock()
        # Guards connect/disconnect/retry transitions, separate from the topic
        # cache lock so a message burst never blocks a reconnect.
        self._state_lock = threading.RLock()
        self._status = "disconnected"
        self._last_error: str | None = None
        self._on_message_cb = on_message
        self._stop = False
        self._reconnect_thread: threading.Thread | None = None
        self._connected_at: float | None = None

    @property
    def status(self) -> str:
        return self._status

    def _set_status(self, status: str) -> None:
        """Single place where status changes, so listeners can react.

        Consumers use this to mark entities unknown when a broker drops.
        """
        previous = self._status
        self._status = status
        if previous == status or self._on_status_change_cb is None:
            return
        try:
            self._on_status_change_cb(self.id, status)
        except Exception:
            logger.exception("on_status_change falló para %s", self.config.label())

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            topic_count = len(self._topics)
            message_total = sum(s.message_count for s in self._topics.values())
        return {
            "id": self.id,
            "name": self.config.name,
            "label": self.config.label(),
            "host": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
            "status": self._status,
            "last_error": self._last_error,
            "topic_count": topic_count,
            "message_total": message_total,
            "connected_at": self._connected_at,
        }

    def connect(self) -> None:
        # Serialized so two quick "Reconectar" clicks can't leave the old retry
        # thread running against the newly built client.
        with self._state_lock:
            self.disconnect()
            self._stop = False
            self._start_client()

    def disconnect(self) -> None:
        self._stop = True
        self._teardown_client()
        self._set_status("disconnected")
        self._connected_at = None

    def reconnect(self) -> None:
        self.connect()

    def _teardown_client(self) -> None:
        """Stop and drop the current paho client, if any."""
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception as exc:
            logger.debug("[%s] disconnect durante teardown: %s", self.config.label(), exc)
        try:
            client.loop_stop()
        except Exception as exc:
            logger.warning(
                "[%s] loop_stop falló durante teardown: %s", self.config.label(), exc
            )

    def _open_client(self) -> None:
        """Build a fresh client and start its network loop."""
        # Distinct client_id per connection: brokers drop the older session when
        # two clients share an id, which would make several connections to the
        # same host fight each other.
        client = mqtt.Client(client_id=f"mqtt_to_entities_{self.id[:8]}")
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        self._client = client
        self._set_status("connecting")

        client.connect_async(self.config.host, self.config.port, keepalive=30)
        client.loop_start()

    def _start_client(self) -> None:
        try:
            self._open_client()
        except Exception as exc:
            self._set_status("error")
            self._last_error = str(exc)
            logger.warning("[%s] connect_async failed: %s", self.config.label(), exc)
            self._schedule_reconnect()

    def _handle_connect(self, client: mqtt.Client, userdata, flags, rc) -> None:
        if rc == 0:
            self._set_status("connected")
            self._last_error = None
            self._connected_at = time.time()
            client.subscribe("#")
            logger.info("[%s] connected and subscribed to #", self.config.label())
        else:
            # The broker refused us (bad credentials, not authorized...). Keep
            # retrying anyway: the cause is often fixable on the broker side, and
            # retries must only stop when the user removes the broker.
            self._set_status("error")
            self._last_error = _connack_message(rc)
            logger.warning("[%s] %s", self.config.label(), self._last_error)
            self._schedule_reconnect()

    def _handle_disconnect(self, client: mqtt.Client, userdata, rc) -> None:
        if self._stop:
            return
        self._set_status("disconnected")
        self._connected_at = None
        logger.warning("[%s] disconnected rc=%s, will retry", self.config.label(), rc)
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        with self._state_lock:
            if self._stop:
                return
            if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
                return
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop, daemon=True
            )
            self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        """Retry forever until connected or the broker is removed/disconnected.

        Rebuilds the paho client on each attempt instead of calling reconnect():
        reconnect() does not restart the network loop, so after a loop_stop() it
        left a socket nobody was reading, the status stuck at "disconnected" and
        no further retries -- the broker never came back on its own.
        """
        backoff = 1
        attempt = 0
        while not self._stop:
            time.sleep(backoff)
            if self._stop:
                return

            attempt += 1
            try:
                self._teardown_client()
                self._open_client()
                # _handle_connect flips the status once the broker answers; give
                # it a moment before deciding this attempt failed.
                deadline = time.monotonic() + CONNECT_WAIT_SECONDS
                while time.monotonic() < deadline:
                    if self._stop or self._status == "connected":
                        return
                    time.sleep(0.2)
                # Report the failure rather than leaving the UI on "conectando"
                # forever; the next iteration flips it back to connecting.
                self._set_status("error")
                self._last_error = (
                    f"sin respuesta de {self.config.label()} "
                    f"(intento {attempt}, reintentando)"
                )
            except Exception as exc:
                self._set_status("error")
                self._last_error = f"{exc} (intento {attempt})"
                logger.warning(
                    "[%s] reintento %s falló: %s", self.config.label(), attempt, exc
                )

            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _handle_message(self, client: mqtt.Client, userdata, msg: mqtt.MQTTMessage) -> None:
        raw = msg.payload.decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            payload = raw

        with self._lock:
            previous = self._topics.get(msg.topic)
            state = TopicState(
                payload=payload,
                raw=raw,
                message_count=(previous.message_count + 1) if previous else 1,
            )
            self._topics[msg.topic] = state

        if self._on_message_cb is not None:
            try:
                self._on_message_cb(self.id, msg.topic, state)
            except Exception:
                logger.exception("on_message callback failed for %s", msg.topic)

    def get_topics(self) -> dict[str, TopicState]:
        with self._lock:
            return dict(self._topics)

    def get_topic(self, topic: str) -> TopicState | None:
        with self._lock:
            return self._topics.get(topic)

    def build_tree(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._topics)
        return _build_tree(snapshot)


class MqttPool:
    """Holds every configured broker connection, keyed by broker id."""

    def __init__(
        self,
        on_message: Callable[[str, str, TopicState], None] | None = None,
        on_status_change: Callable[[str, str], None] | None = None,
    ) -> None:
        self._connections: dict[str, BrokerConnection] = {}
        self._lock = threading.Lock()
        self._on_message_cb = on_message
        self._on_status_change_cb = on_status_change

    def add(self, config: BrokerConfig, broker_id: str | None = None, connect: bool = True) -> BrokerConnection:
        broker_id = broker_id or str(uuid.uuid4())
        connection = BrokerConnection(
            broker_id,
            config,
            on_message=self._on_message_cb,
            on_status_change=self._on_status_change_cb,
        )
        with self._lock:
            self._connections[broker_id] = connection
        if connect:
            connection.connect()
        return connection

    def get(self, broker_id: str) -> BrokerConnection | None:
        with self._lock:
            return self._connections.get(broker_id)

    def list(self) -> list[BrokerConnection]:
        with self._lock:
            return list(self._connections.values())

    def remove(self, broker_id: str) -> bool:
        with self._lock:
            connection = self._connections.pop(broker_id, None)
        if connection is None:
            return False
        connection.disconnect()
        return True

    def update(self, broker_id: str, config: BrokerConfig) -> BrokerConnection | None:
        with self._lock:
            connection = self._connections.get(broker_id)
        if connection is None:
            return None
        # Reconnecting with the new settings is the only way to change host or
        # credentials, so the topic cache starts fresh for the new target.
        connection.disconnect()
        return self.add(config, broker_id=broker_id, connect=True)

    def find_by_endpoint(self, host: str, port: int) -> BrokerConnection | None:
        for connection in self.list():
            if connection.config.host == host and connection.config.port == port:
                return connection
        return None

    def configs(self) -> list[dict[str, Any]]:
        return [
            {"id": c.id, **asdict(c.config)}
            for c in self.list()
        ]


def _connack_message(rc: Any) -> str:
    messages = {
        1: "protocolo incorrecto",
        2: "client id rechazado",
        3: "broker no disponible",
        4: "usuario o contraseña incorrectos",
        5: "no autorizado",
    }
    return f"conexión rechazada: {messages.get(rc, f'rc={rc}')}"


def _preview(raw: str) -> str:
    collapsed = " ".join(raw.split())
    if len(collapsed) <= PREVIEW_MAX_CHARS:
        return collapsed
    return collapsed[:PREVIEW_MAX_CHARS] + "…"


def _build_tree(topics: dict[str, TopicState]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for topic, state in topics.items():
        segments = topic.split("/")
        node = tree
        for segment in segments:
            node = node.setdefault("children", {}).setdefault(segment, {})
        node["__topic__"] = topic
        node["__message_count__"] = state.message_count
        node["__preview__"] = _preview(state.raw)

    _annotate_totals(tree)
    return tree


def _annotate_totals(node: dict[str, Any]) -> tuple[int, int]:
    """Attach descendant-topic and total-message counts to every node.

    Returns the (topic_count, message_count) subtree totals so parents can
    accumulate their children's numbers, mirroring MQTT Explorer's display.
    """
    topics = 1 if "__topic__" in node else 0
    messages = node.get("__message_count__", 0)

    children = node.get("children")
    if children:
        for child in children.values():
            child_topics, child_messages = _annotate_totals(child)
            topics += child_topics
            messages += child_messages
        node["__child_count__"] = len(children)

    node["__topic_total__"] = topics
    node["__message_total__"] = messages
    return topics, messages
