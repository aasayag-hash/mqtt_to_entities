from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import domain_transform, ha_api, mappings_store
from backend.json_paths import flatten_paths, resolve_path
from backend.mqtt_client import BrokerConfig, MqttManager, TopicState

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mqtt_to_entities.main")

# Home Assistant's ingress proxy (Supervisor) strips the "/api/hassio_ingress/<token>"
# prefix before forwarding the request to the add-on container, so the app is written
# to serve everything relative to "/" and does not need to know the ingress prefix.
app = FastAPI(title="MQTT to Entities")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

VALID_DOMAINS = {"sensor", "binary_sensor", "switch", "number", "text", "select"}


def _on_mqtt_message(topic: str, state: TopicState) -> None:
    for mapping in mappings_store.list_mappings():
        if mapping["topic"] != topic:
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
        return

    if ha_api.set_state(mapping["entity_id"], value, attributes):
        mappings_store.set_last_value(mapping["id"], value)


mqtt_manager = MqttManager(on_message=_on_mqtt_message)


class BrokerConfigIn(BaseModel):
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None


class MappingIn(BaseModel):
    topic: str
    field_path: str
    entity_id: str
    domain: str
    domain_config: dict[str, Any] = {}


class MappingUpdate(BaseModel):
    topic: str | None = None
    field_path: str | None = None
    entity_id: str | None = None
    domain: str | None = None
    domain_config: dict[str, Any] | None = None


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    return {"status": mqtt_manager.status, "last_error": mqtt_manager.last_error}


@app.post("/api/connect")
def connect(config: BrokerConfigIn) -> dict[str, Any]:
    mqtt_manager.connect(
        BrokerConfig(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
        )
    )
    return {"status": mqtt_manager.status}


@app.post("/api/reconnect")
def reconnect() -> dict[str, Any]:
    mqtt_manager.reconnect()
    return {"status": mqtt_manager.status}


@app.post("/api/disconnect")
def disconnect() -> dict[str, Any]:
    mqtt_manager.disconnect()
    return {"status": mqtt_manager.status}


@app.get("/api/tree")
def get_tree() -> dict[str, Any]:
    return mqtt_manager.build_tree()


@app.get("/api/topics")
def get_topics() -> list[str]:
    return sorted(mqtt_manager.get_topics().keys())


@app.get("/api/topics/{topic:path}")
def get_topic(topic: str) -> dict[str, Any]:
    state = mqtt_manager.get_topic(topic)
    if state is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    paths = flatten_paths(state.payload) if isinstance(state.payload, (dict, list)) else []
    return {
        "topic": topic,
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
    return mappings_store.create_mapping(mapping.model_dump())


@app.put("/api/mappings/{mapping_id}")
def update_mapping(mapping_id: str, updates: MappingUpdate) -> dict[str, Any]:
    payload = {k: v for k, v in updates.model_dump().items() if v is not None}
    if "domain" in payload and payload["domain"] not in VALID_DOMAINS:
        raise HTTPException(status_code=400, detail=f"Invalid domain: {payload['domain']}")
    result = mappings_store.update_mapping(mapping_id, payload)
    if result is None:
        raise HTTPException(status_code=404, detail="Mapping not found")
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
