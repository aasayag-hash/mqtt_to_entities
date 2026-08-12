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
    ) -> None:
        self.id = broker_id
        self.config = config
        self._client: mqtt.Client | None = None
        self._topics: dict[str, TopicState] = {}
        self._lock = threading.Lock()
        self._status = "disconnected"
        self._last_error: str | None = None
        self._on_message_cb = on_message
        self._stop = False
        self._reconnect_thread: threading.Thread | None = None
        self._connected_at: float | None = None

    @property
    def status(self) -> str:
        return self._status

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
        self.disconnect()
        self._stop = False
        self._start_client()

    def disconnect(self) -> None:
        self._stop = True
        if self._client is not None:
            try:
                self._client.disconnect()
                self._client.loop_stop()
            except Exception:
                pass
            self._client = None
        self._status = "disconnected"
        self._connected_at = None

    def reconnect(self) -> None:
        self.connect()

    def _start_client(self) -> None:
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

        try:
            client.connect_async(self.config.host, self.config.port, keepalive=30)
            client.loop_start()
            self._status = "connecting"
        except Exception as exc:
            self._status = "error"
            self._last_error = str(exc)
            logger.warning("[%s] connect_async failed: %s", self.config.label(), exc)
            self._schedule_reconnect()

    def _handle_connect(self, client: mqtt.Client, userdata, flags, rc) -> None:
        if rc == 0:
            self._status = "connected"
            self._last_error = None
            self._connected_at = time.time()
            client.subscribe("#")
            logger.info("[%s] connected and subscribed to #", self.config.label())
        else:
            self._status = "error"
            self._last_error = _connack_message(rc)

    def _handle_disconnect(self, client: mqtt.Client, userdata, rc) -> None:
        if self._stop:
            return
        self._status = "disconnected"
        self._connected_at = None
        logger.warning("[%s] disconnected rc=%s, will retry", self.config.label(), rc)
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._stop:
            return
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        backoff = 1
        while not self._stop and self._status != "connected":
            time.sleep(backoff)
            if self._stop:
                return
            try:
                if self._client is not None:
                    self._client.reconnect()
                    return
            except Exception as exc:
                self._last_error = str(exc)
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

    def __init__(self, on_message: Callable[[str, str, TopicState], None] | None = None) -> None:
        self._connections: dict[str, BrokerConnection] = {}
        self._lock = threading.Lock()
        self._on_message_cb = on_message

    def add(self, config: BrokerConfig, broker_id: str | None = None, connect: bool = True) -> BrokerConnection:
        broker_id = broker_id or str(uuid.uuid4())
        connection = BrokerConnection(broker_id, config, on_message=self._on_message_cb)
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
