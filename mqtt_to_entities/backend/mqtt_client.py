from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
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


@dataclass
class TopicState:
    payload: Any
    raw: str
    received_at: float = field(default_factory=time.time)
    message_count: int = 1


class MqttManager:
    def __init__(self, on_message: Callable[[str, TopicState], None] | None = None) -> None:
        self._client: mqtt.Client | None = None
        self._config: BrokerConfig | None = None
        self._topics: dict[str, TopicState] = {}
        self._lock = threading.Lock()
        self._status = "disconnected"
        self._last_error: str | None = None
        self._on_message_cb = on_message
        self._stop = False
        self._reconnect_thread: threading.Thread | None = None

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def connect(self, config: BrokerConfig) -> None:
        self.disconnect()
        self._config = config
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

    def reconnect(self) -> None:
        if self._config is None:
            return
        self.connect(self._config)

    def _start_client(self) -> None:
        assert self._config is not None
        client = mqtt.Client()
        if self._config.username:
            client.username_pw_set(self._config.username, self._config.password)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        self._client = client

        try:
            client.connect_async(self._config.host, self._config.port, keepalive=30)
            client.loop_start()
        except Exception as exc:
            self._status = "error"
            self._last_error = str(exc)
            logger.warning("MQTT connect_async failed: %s", exc)
            self._schedule_reconnect()

    def _handle_connect(self, client: mqtt.Client, userdata, flags, rc) -> None:
        if rc == 0:
            self._status = "connected"
            self._last_error = None
            client.subscribe("#")
            logger.info("MQTT connected and subscribed to #")
        else:
            self._status = "error"
            self._last_error = f"connect rc={rc}"

    def _handle_disconnect(self, client: mqtt.Client, userdata, rc) -> None:
        if self._stop:
            return
        self._status = "disconnected"
        logger.warning("MQTT disconnected rc=%s, will retry", rc)
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
            if self._stop or self._config is None:
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
                self._on_message_cb(msg.topic, state)
            except Exception:
                logger.exception("on_message callback failed for topic %s", msg.topic)

    def get_topics(self) -> dict[str, TopicState]:
        with self._lock:
            return dict(self._topics)

    def get_topic(self, topic: str) -> TopicState | None:
        with self._lock:
            return self._topics.get(topic)

    def build_tree(self) -> dict[str, Any]:
        tree: dict[str, Any] = {}
        with self._lock:
            snapshot = dict(self._topics)

        for topic, state in snapshot.items():
            segments = topic.split("/")
            node = tree
            for segment in segments:
                node = node.setdefault("children", {}).setdefault(segment, {})
            node["__topic__"] = topic
            node["__message_count__"] = state.message_count
            node["__preview__"] = _preview(state.raw)

        _annotate_totals(tree)
        return tree


def _preview(raw: str) -> str:
    collapsed = " ".join(raw.split())
    if len(collapsed) <= PREVIEW_MAX_CHARS:
        return collapsed
    return collapsed[:PREVIEW_MAX_CHARS] + "…"


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
