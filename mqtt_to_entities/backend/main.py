from __future__ import annotations

import logging
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


def _on_mqtt_message(broker_id: str, topic: str, state: TopicState) -> None:
    for mapping in mappings_store.list_mappings():
        if mapping["topic"] != topic:
            continue
        # Mappings are bound to one broker; legacy rows without broker_id match
        # any broker so they keep working until edited.
        mapping_broker = mapping.get("broker_id")
        if mapping_broker and mapping_broker != broker_id:
            continue
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


mqtt_pool = MqttPool(on_message=_on_mqtt_message)


def _persist_brokers() -> None:
    brokers_store.save_brokers(mqtt_pool.configs())


@app.on_event("startup")
def _restore_brokers() -> None:
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


@app.on_event("startup")
def _restore_last_values() -> None:
    # last_value is already the post-transform state; re-push it as-is so the
    # entity keeps its last known value across HA Core / add-on restarts
    # instead of disappearing until the next matching MQTT message arrives.
    for mapping in mappings_store.list_mappings():
        last_value = mapping.get("last_value")
        if last_value is None:
            continue
        ok, error = ha_api.set_state(mapping["entity_id"], last_value)
        if not ok:
            mappings_store.set_last_error(mapping["id"], error)


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
