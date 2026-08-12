from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import brokers_store, domain_transform, ha_api, mappings_store
from backend.json_paths import flatten_paths, resolve_path
from backend.mqtt_client import BrokerConfig, MqttPool, TopicState

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mqtt_to_entities.main")

# Home Assistant's ingress proxy (Supervisor) strips the "/api/hassio_ingress/<token>"
# prefix before forwarding the request to the add-on container, so the app is written
# to serve everything relative to "/" and does not need to know the ingress prefix.
app = FastAPI(title="MQTT to Entities")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

VALID_DOMAINS = {"sensor", "binary_sensor", "switch", "number", "text", "select"}

# Home Assistant's reserved state for "no data"; shown as desconocido in the UI.
HA_UNKNOWN_STATE = "unknown"

# Set once the saved values have been restored and the brokers have been started,
# so the initial connection transitions don't blank the restored values.
_startup_done = False

# A topic that stops publishing leaves its entity showing a stale value, so each
# mapping goes unknown after this long without data unless it overrides it.
DEFAULT_STALE_TIMEOUT_SECONDS = 300.0
STALE_CHECK_INTERVAL_SECONDS = 30.0

_shutdown = threading.Event()


def _on_mqtt_message(broker_id: str, topic: str, state: TopicState) -> None:
    for mapping in mappings_store.mappings_for_topic(topic, broker_id):
        _apply_mapping(mapping, state)


def _apply_mapping(mapping: dict[str, Any], state: TopicState) -> None:
    if not isinstance(state.payload, (dict, list)):
        return

    raw_value = resolve_path(state.payload, mapping["field_path"])
    if raw_value is None:
        return

    try:
        value, attributes = domain_transform.transform(
            mapping["domain"], mapping.get("domain_config", {}), raw_value
        )
    except domain_transform.TransformError as exc:
        logger.info("Skipping mapping %s: %s", mapping["id"], exc)
        mappings_store.set_last_error(mapping["id"], str(exc))
        return

    ok, error = ha_api.set_state(mapping["entity_id"], value, attributes)
    if ok:
        mappings_store.set_last_value(mapping["id"], value)
    else:
        mappings_store.set_last_error(mapping["id"], error)


def _mark_unknown(mapping: dict[str, Any], reason: str) -> None:
    """Push Home Assistant's "unknown" state for one mapping."""
    if mapping.get("last_value") == HA_UNKNOWN_STATE:
        return  # already blanked; don't spam the API

    ok, error = ha_api.set_state(mapping["entity_id"], HA_UNKNOWN_STATE)
    if ok:
        logger.info("%s -> unknown (%s)", mapping["entity_id"], reason)
        mappings_store.set_unknown(mapping["id"], HA_UNKNOWN_STATE)
    else:
        mappings_store.set_last_error(mapping["id"], error)


def _stale_timeout(mapping: dict[str, Any]) -> float | None:
    """Seconds of silence before this mapping goes unknown, or None to disable."""
    raw = mapping.get("domain_config", {}).get("stale_timeout")
    if raw is None:
        raw = DEFAULT_STALE_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_STALE_TIMEOUT_SECONDS
    return timeout if timeout > 0 else None


def _check_stale_mappings() -> None:
    """Blank entities whose topic stopped publishing.

    The broker can stay connected while one device goes quiet (a Victron unit
    leaving the bus), in which case the entity would otherwise keep showing its
    last value forever.
    """
    now = time.time()
    for mapping in mappings_store.list_mappings():
        timeout = _stale_timeout(mapping)
        if timeout is None:
            continue
        if mapping.get("last_value") in (None, HA_UNKNOWN_STATE):
            continue

        last_update = mapping.get("last_update_at")
        if last_update is None:
            # Pre-existing mapping from before this field existed: start its
            # clock now instead of blanking it immediately.
            mappings_store.set_last_update_at(mapping["id"], now)
            continue

        if now - last_update > timeout:
            _mark_unknown(mapping, f"sin datos por {int(now - last_update)}s")


def _stale_watchdog() -> None:
    while not _shutdown.wait(STALE_CHECK_INTERVAL_SECONDS):
        try:
            _check_stale_mappings()
        except Exception:
            logger.exception("El chequeo de entidades sin datos falló")


def _on_broker_status_change(broker_id: str, status: str) -> None:
    """Mark a broker's entities unknown while it is not connected.

    Without this the entities keep showing the last value they ever got, which
    looks like live data long after the broker went away. Home Assistant renders
    the literal state "unknown" as unavailable/desconocido.
    """
    if status == "connected":
        return
    # The very first "connecting" happens while restoring saved values; blanking
    # them there would defeat the restore.
    if not _startup_done:
        return

    for mapping in mappings_store.list_mappings():
        # Legacy mappings without broker_id are fed by any broker, so another
        # one may still be publishing; leave those alone.
        if mapping.get("broker_id") != broker_id:
            continue
        _mark_unknown(mapping, f"broker {status}")


mqtt_pool = MqttPool(
    on_message=_on_mqtt_message,
    on_status_change=_on_broker_status_change,
)


def _persist_brokers() -> None:
    brokers_store.save_brokers(mqtt_pool.configs())


@app.on_event("startup")
def _restore_last_values() -> None:
    # Runs before the brokers connect: last_value is already the post-transform
    # state, so re-pushing it keeps the entity's last known value across
    # restarts instead of it disappearing until the next matching message. A
    # stored "unknown" is re-pushed as-is, which is what we want.
    for mapping in mappings_store.list_mappings():
        last_value = mapping.get("last_value")
        if last_value is None:
            continue
        ok, error = ha_api.set_state(mapping["entity_id"], last_value)
        if not ok:
            mappings_store.set_last_error(mapping["id"], error)


@app.on_event("startup")
def _restore_brokers() -> None:
    # Registered after _restore_last_values so the initial "connecting"
    # transition cannot blank out the values that were just restored.
    global _startup_done
    for entry in brokers_store.list_brokers():
        config = BrokerConfig(
            host=entry.get("host", ""),
            port=entry.get("port", 1883),
            username=entry.get("username"),
            password=entry.get("password"),
            name=entry.get("name"),
        )
        if not config.host:
            continue
        mqtt_pool.add(config, broker_id=entry.get("id"), connect=True)

    # From here on, losing a broker should blank its entities.
    _startup_done = True

    threading.Thread(target=_stale_watchdog, daemon=True, name="stale-watchdog").start()


@app.on_event("shutdown")
def _flush_on_shutdown() -> None:
    # last_value/last_error are batched in memory; make sure the newest ones
    # reach disk when the add-on stops cleanly.
    _shutdown.set()
    mappings_store.flush()


class BrokerConfigIn(BaseModel):
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    name: str | None = None


class MappingIn(BaseModel):
    topic: str
    field_path: str
    entity_id: str
    domain: str
    domain_config: dict[str, Any] = {}
    broker_id: str | None = None


class MappingUpdate(BaseModel):
    topic: str | None = None
    field_path: str | None = None
    entity_id: str | None = None
    domain: str | None = None
    domain_config: dict[str, Any] | None = None
    broker_id: str | None = None


def _to_broker_config(config: BrokerConfigIn) -> BrokerConfig:
    return BrokerConfig(
        host=config.host,
        port=config.port,
        username=config.username,
        password=config.password,
        name=config.name,
    )


def _resolve_broker(broker_id: str | None):
    """Return the requested broker, or the only one when none is specified."""
    if broker_id:
        connection = mqtt_pool.get(broker_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="Broker no encontrado")
        return connection

    connections = mqtt_pool.list()
    if not connections:
        return None
    return connections[0]


@app.get("/api/brokers")
def list_brokers() -> list[dict[str, Any]]:
    return [c.to_dict() for c in mqtt_pool.list()]


@app.post("/api/brokers")
def add_broker(config: BrokerConfigIn) -> dict[str, Any]:
    if not config.host.strip():
        raise HTTPException(status_code=400, detail="El host no puede estar vacío")

    existing = mqtt_pool.find_by_endpoint(config.host, config.port)
    if existing is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Ya existe un broker para {config.host}:{config.port}",
        )

    connection = mqtt_pool.add(_to_broker_config(config))
    _persist_brokers()
    return connection.to_dict()


@app.put("/api/brokers/{broker_id}")
def update_broker(broker_id: str, config: BrokerConfigIn) -> dict[str, Any]:
    if not config.host.strip():
        raise HTTPException(status_code=400, detail="El host no puede estar vacío")

    existing = mqtt_pool.get(broker_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Broker no encontrado")

    duplicate = mqtt_pool.find_by_endpoint(config.host, config.port)
    if duplicate is not None and duplicate.id != broker_id:
        raise HTTPException(
            status_code=400,
            detail=f"Ya existe otro broker para {config.host}:{config.port}",
        )

    new_config = _to_broker_config(config)
    # The API never returns stored passwords, so an omitted/blank one on edit
    # means "keep the current password" rather than "clear it".
    if new_config.password is None:
        new_config.password = existing.config.password

    connection = mqtt_pool.update(broker_id, new_config)
    if connection is None:
        raise HTTPException(status_code=404, detail="Broker no encontrado")
    _persist_brokers()
    return connection.to_dict()


@app.delete("/api/brokers/{broker_id}")
def delete_broker(broker_id: str) -> dict[str, Any]:
    if not mqtt_pool.remove(broker_id):
        raise HTTPException(status_code=404, detail="Broker no encontrado")
    _persist_brokers()
    return {"deleted": True}


@app.post("/api/brokers/{broker_id}/reconnect")
def reconnect_broker(broker_id: str) -> dict[str, Any]:
    connection = mqtt_pool.get(broker_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Broker no encontrado")
    connection.reconnect()
    return connection.to_dict()


@app.post("/api/brokers/{broker_id}/disconnect")
def disconnect_broker(broker_id: str) -> dict[str, Any]:
    connection = mqtt_pool.get(broker_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Broker no encontrado")
    connection.disconnect()
    return connection.to_dict()


@app.get("/api/tree")
def get_tree(broker_id: str | None = None) -> dict[str, Any]:
    connection = _resolve_broker(broker_id)
    if connection is None:
        return {}
    return connection.build_tree()


@app.get("/api/topics")
def get_topics(broker_id: str | None = None) -> list[str]:
    connection = _resolve_broker(broker_id)
    if connection is None:
        return []
    return sorted(connection.get_topics().keys())


@app.get("/api/topics/{topic:path}")
def get_topic(topic: str, broker_id: str | None = None) -> dict[str, Any]:
    connection = _resolve_broker(broker_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="No hay brokers configurados")

    state = connection.get_topic(topic)
    if state is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    paths = flatten_paths(state.payload) if isinstance(state.payload, (dict, list)) else []
    return {
        "topic": topic,
        "broker_id": connection.id,
        "payload": state.payload,
        "raw": state.raw,
        "field_paths": paths,
        "received_at": state.received_at,
    }


@app.get("/api/mappings")
def list_mappings() -> list[dict[str, Any]]:
    return mappings_store.list_mappings()


@app.post("/api/mappings")
def create_mapping(mapping: MappingIn) -> dict[str, Any]:
    if mapping.domain not in VALID_DOMAINS:
        raise HTTPException(status_code=400, detail=f"Invalid domain: {mapping.domain}")
    error = ha_api.validate_entity_id(mapping.entity_id, mapping.domain)
    if error:
        raise HTTPException(status_code=400, detail=error)

    created = mappings_store.create_mapping(mapping.model_dump())

    # Push immediately from the latest retained payload so the entity shows a
    # value right away instead of waiting for the next MQTT message.
    connection = _resolve_broker(mapping.broker_id)
    state = connection.get_topic(mapping.topic) if connection else None
    if state is not None:
        _apply_mapping(created, state)
        created = mappings_store.get_mapping(created["id"]) or created
    return created


@app.put("/api/mappings/{mapping_id}")
def update_mapping(mapping_id: str, updates: MappingUpdate) -> dict[str, Any]:
    payload = {k: v for k, v in updates.model_dump().items() if v is not None}
    if "domain" in payload and payload["domain"] not in VALID_DOMAINS:
        raise HTTPException(status_code=400, detail=f"Invalid domain: {payload['domain']}")

    existing = mappings_store.get_mapping(mapping_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Mapping not found")

    entity_id = payload.get("entity_id", existing["entity_id"])
    domain = payload.get("domain", existing["domain"])
    error = ha_api.validate_entity_id(entity_id, domain)
    if error:
        raise HTTPException(status_code=400, detail=error)

    result = mappings_store.update_mapping(mapping_id, payload)
    if result is None:
        raise HTTPException(status_code=404, detail="Mapping not found")

    connection = _resolve_broker(result.get("broker_id"))
    state = connection.get_topic(result["topic"]) if connection else None
    if state is not None:
        _apply_mapping(result, state)
        result = mappings_store.get_mapping(mapping_id) or result
    return result


@app.delete("/api/mappings/{mapping_id}")
def delete_mapping(mapping_id: str) -> dict[str, Any]:
    if not mappings_store.delete_mapping(mapping_id):
        raise HTTPException(status_code=404, detail="Mapping not found")
    return {"deleted": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
