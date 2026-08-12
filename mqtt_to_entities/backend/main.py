from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import brokers_store, domain_transform, ha_api, mappings_store, network_scan
from backend.json_paths import flatten_paths, resolve_path
from backend.mqtt_client import BrokerConfig, DuplicateBrokerError, MqttPool, TopicState

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

# Brokers whose entities still need to be blanked. Filled from paho's network
# threads, drained by _status_worker so the HTTP calls never run there.
_pending_unknown: set[str] = set()
_pending_lock = threading.Lock()
_status_work = threading.Event()


def _on_mqtt_message(broker_id: str, topic: str, state: TopicState) -> None:
    for mapping in mappings_store.mappings_for_topic(topic, broker_id):
        _apply_mapping(mapping, state)


def _apply_mapping(mapping: dict[str, Any], state: TopicState) -> None:
    field_path = mapping.get("field_path") or ""

    if isinstance(state.payload, (dict, list)):
        raw_value = resolve_path(state.payload, field_path)
        if raw_value is None:
            # Distinguish "the path isn't there" from "the value really is null",
            # instead of silently doing nothing in both cases.
            available = flatten_paths(state.payload)
            if field_path and field_path not in available:
                hint = ", ".join(available[:5]) or "ninguno"
                mappings_store.set_last_error(
                    mapping["id"],
                    f"El campo '{field_path}' no está en el payload. Campos disponibles: {hint}",
                )
                return
            raw_value = None
    else:
        # Scalar payload (Victron publishes plain values on some topics). Only a
        # root mapping can consume it; a field path has nothing to resolve.
        if field_path:
            mappings_store.set_last_error(
                mapping["id"],
                f"El payload de este topic no es JSON ({state.raw[:40]!r}), así que no "
                f"tiene el campo '{field_path}'. Mapeá el valor completo en su lugar.",
            )
            return
        raw_value = state.payload

    if raw_value is None:
        mappings_store.set_last_error(
            mapping["id"], "El valor recibido es null"
        )
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
    """Push Home Assistant's "unknown" state for one mapping, at most once.

    The attempt is recorded whether or not the push succeeded. Keying only on
    success meant that with Home Assistant unreachable, every status transition
    retried every entity -- 2xN blocking HTTP calls per retry cycle, forever.
    A later successful message clears the flag, so it recovers on its own.
    """
    if mapping.get("last_value") == HA_UNKNOWN_STATE or mapping.get("unknown_pushed"):
        return  # already blanked (or already attempted); don't spam the API

    ok, error = ha_api.set_state(mapping["entity_id"], HA_UNKNOWN_STATE)
    if ok:
        logger.info("%s -> unknown (%s)", mapping["entity_id"], reason)
        mappings_store.set_unknown(mapping["id"], HA_UNKNOWN_STATE)
    else:
        logger.warning("No se pudo marcar %s como unknown: %s", mapping["entity_id"], error)
        mappings_store.mark_unknown_attempted(mapping["id"], error)


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


# Consecutive failure counts per watchdog task, so a permanent fault (read-only
# /data, full disk) is reported once instead of every 30s forever.
_watchdog_failures: dict[str, int] = {}

# Consecutive successes per task since the last failure. A single success used to
# reset the failure count, so an intermittent fault (ENOSPC hovering at the
# threshold) alternated failure/success and logged a fresh traceback every other
# tick -- more noise than before the suppression existed.
_watchdog_successes: dict[str, int] = {}

# After this many consecutive failures, stop logging until it recovers.
MAX_REPEATED_FAILURE_LOGS = 3

# Successes required before a task is considered healthy again and its failure
# count is cleared.
SUCCESSES_TO_CLEAR_FAILURES = 3


def _run_watchdog_task(name: str, task: Callable[[], None], message: str) -> None:
    """Run one watchdog step, logging failures without flooding the log.

    A read-only /data made this emit a full traceback every 30s forever, which
    buries every other message in the add-on log (and fills the disk further
    when the cause was a full disk). The first few failures are logged, then it
    goes quiet until the condition clears.
    """
    try:
        task()
    except Exception as exc:
        _watchdog_successes.pop(name, None)
        failures = _watchdog_failures.get(name, 0) + 1
        _watchdog_failures[name] = failures
        if failures == 1:
            # Full traceback once, when it is still news.
            logger.exception(message)
        elif failures <= MAX_REPEATED_FAILURE_LOGS:
            logger.warning("%s: %s (fallo %d)", message, exc, failures)
        elif failures == MAX_REPEATED_FAILURE_LOGS + 1:
            logger.warning(
                "%s: %s. Se dejará de informar hasta que se resuelva.", message, exc
            )
        return

    if name not in _watchdog_failures:
        return

    # Require a few clean runs before declaring recovery, so a flapping fault
    # cannot re-arm the traceback on every other tick.
    successes = _watchdog_successes.get(name, 0) + 1
    if successes < SUCCESSES_TO_CLEAR_FAILURES:
        _watchdog_successes[name] = successes
        return

    _watchdog_successes.pop(name, None)
    failures = _watchdog_failures.pop(name, 0)
    if failures:
        # Phrased as a recovery, not by reusing the failure message: reporting
        # "El chequeo ... falló: resuelto." read as a contradiction and made a
        # grep for the failure text hit the success line too.
        logger.info(
            "Resuelto (tras %d fallo%s): %s",
            failures,
            "" if failures == 1 else "s",
            message,
        )


def _stale_watchdog() -> None:
    while not _shutdown.wait(STALE_CHECK_INTERVAL_SECONDS):
        _run_watchdog_task(
            "stale_check", _check_stale_mappings, "El chequeo de entidades sin datos falló"
        )
        # last_update_at is otherwise only flushed on a clean shutdown, which
        # never runs on SIGKILL/OOM. An add-on that restarts more often than
        # that would keep resetting every mapping's staleness clock and never
        # detect a dead topic, so piggyback a flush here.
        _run_watchdog_task(
            "flush", mappings_store.flush, "No se pudo guardar el estado de las entidades"
        )


def _on_broker_status_change(broker_id: str, status: str) -> None:
    """Queue a broker's entities to be blanked; never do I/O on this thread.

    This runs on paho's network thread (and inside the retry loop), so it only
    enqueues. Pushing to Home Assistant here meant a 5s-timeout HTTP call per
    entity was stalling connection handling and the retry backoff.
    """
    if status == "connected":
        # Coming back up cancels a pending blanking that never got processed.
        with _pending_lock:
            _pending_unknown.discard(broker_id)
        return
    # The very first "connecting" happens while restoring saved values; blanking
    # them there would defeat the restore.
    if not _startup_done:
        return

    with _pending_lock:
        _pending_unknown.add(broker_id)
    _status_work.set()


def _blank_broker_entities(broker_id: str) -> None:
    """Push "unknown" for a broker's entities, unless it reconnected meanwhile."""
    connection = mqtt_pool.get(broker_id)
    if connection is not None and connection.status == "connected":
        return

    label = connection.config.label() if connection is not None else broker_id
    for mapping in mappings_store.list_mappings():
        # Legacy mappings without broker_id are fed by any broker, so another
        # one may still be publishing; leave those alone.
        if mapping.get("broker_id") != broker_id:
            continue
        _mark_unknown(mapping, f"broker {label} sin conexión")


def _status_worker() -> None:
    """Serializes the slow HTTP work triggered by broker status changes."""
    while not _shutdown.is_set():
        _status_work.wait(timeout=1.0)
        _status_work.clear()
        while not _shutdown.is_set():
            with _pending_lock:
                if not _pending_unknown:
                    break
                broker_id = _pending_unknown.pop()
            try:
                _blank_broker_entities(broker_id)
            except Exception:
                logger.exception("Fallo al marcar entidades de %s", broker_id)


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
            subscribe_sys=entry.get("subscribe_sys", False),
        )
        if not config.host:
            continue
        try:
            # A hand-edited brokers.json can list the same endpoint twice; the
            # UI cannot produce that, but restoring both would open two
            # competing connections to it.
            mqtt_pool.add(
                config, broker_id=entry.get("id"), connect=True, check_duplicate=True
            )
        except DuplicateBrokerError as exc:
            logger.warning(
                "Se ignora el broker duplicado %s (ya existe %s)",
                config.label(),
                exc.existing.id,
            )

    # From here on, losing a broker should blank its entities.
    _startup_done = True

    threading.Thread(target=_stale_watchdog, daemon=True, name="stale-watchdog").start()
    threading.Thread(target=_status_worker, daemon=True, name="status-worker").start()


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
    subscribe_sys: bool = False


class ScanRequest(BaseModel):
    # Empty means "autodetect the add-on's own /24".
    range: str | None = None


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
        subscribe_sys=config.subscribe_sys,
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

    try:
        connection = mqtt_pool.add(_to_broker_config(config), check_duplicate=True)
    except DuplicateBrokerError:
        raise HTTPException(
            status_code=400,
            detail=f"Ya existe un broker para {config.host}:{config.port}",
        )

    try:
        _persist_brokers()
    except OSError as exc:
        # The broker is already registered and connecting, so a failed save
        # would leave it working in the UI until the next restart, when it
        # silently disappears. Undo it instead of reporting a half-done add.
        logger.error("No se pudo guardar el broker; se descarta: %s", exc)
        mqtt_pool.remove(connection.id)
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo guardar la configuración del broker: {exc}",
        )
    return connection.to_dict()


@app.put("/api/brokers/{broker_id}")
def update_broker(broker_id: str, config: BrokerConfigIn) -> dict[str, Any]:
    if not config.host.strip():
        raise HTTPException(status_code=400, detail="El host no puede estar vacío")

    existing = mqtt_pool.get(broker_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Broker no encontrado")

    new_config = _to_broker_config(config)
    # The API never returns stored passwords, so an omitted/blank one on edit
    # means "keep the current password" rather than "clear it".
    if new_config.password is None:
        new_config.password = existing.config.password

    # The duplicate check happens inside update(), under the pool lock: checking
    # here first let two concurrent edits both pass and create two connections
    # to the same endpoint.
    try:
        connection = mqtt_pool.update(broker_id, new_config, check_duplicate=True)
    except DuplicateBrokerError:
        raise HTTPException(
            status_code=400,
            detail=f"Ya existe otro broker para {config.host}:{config.port}",
        )
    if connection is None:
        raise HTTPException(status_code=404, detail="Broker no encontrado")

    try:
        _persist_brokers()
    except OSError as exc:
        # No rollback here: the previous connection is already torn down, so
        # removing the replacement would drop the broker altogether. Keep the
        # working connection and report that only the save failed.
        logger.error("El broker se actualizó pero no se pudo guardar: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=(
                f"El broker se actualizó pero no se pudo guardar en disco ({exc}). "
                "Los cambios se perderán al reiniciar el add-on."
            ),
        )
    return connection.to_dict()


@app.delete("/api/brokers/{broker_id}")
def delete_broker(broker_id: str) -> dict[str, Any]:
    if not mqtt_pool.remove(broker_id):
        raise HTTPException(status_code=404, detail="Broker no encontrado")

    try:
        _persist_brokers()
    except OSError as exc:
        # The connection is already closed and cannot be restored, so the
        # deletion stands; only warn that it will come back on restart.
        logger.error("El broker se eliminó pero no se pudo guardar: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=(
                f"El broker se eliminó pero no se pudo guardar en disco ({exc}). "
                "Volverá a aparecer al reiniciar el add-on."
            ),
        )
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


def _client_lan_hint(request: Request) -> str | None:
    """The browser's own LAN /24, deduced from how it reached Home Assistant.

    The add-on container only sees Docker's internal network, so its own address
    is useless as a scan default. The Host header (or the peer address) is the
    address the user typed to reach HA, which is on the network they want
    scanned.
    """
    for candidate in (
        request.headers.get("x-forwarded-host"),
        request.headers.get("host"),
        request.client.host if request.client else None,
    ):
        hint = network_scan.cidr_from_address(candidate)
        if hint:
            return hint
    return None


@app.get("/api/scan/default-range")
def scan_default_range(request: Request) -> dict[str, Any]:
    """Range suggestions for the UI, best guess first.

    "range" is the range to prefill: the browser's own LAN when it can be
    deduced, otherwise whatever the add-on can detect. "suggestions" always
    carries usable options even when nothing could be detected.
    """
    hint = _client_lan_hint(request)
    return {
        "range": hint or network_scan.local_cidr(),
        "suggestions": network_scan.suggested_ranges(
            request.headers.get("x-forwarded-host") or request.headers.get("host")
        ),
    }


@app.post("/api/scan")
async def scan_network(request: Request, body: ScanRequest) -> dict[str, Any]:
    """Sweep a local range for MQTT brokers so the user needn't know the IP."""
    # Fall back to the browser's LAN before giving up: an empty field should
    # scan something sensible rather than return an error.
    cidr = (
        body.range
        or _client_lan_hint(request)
        or network_scan.local_cidr()
        or ""
    ).strip()
    if not cidr:
        raise HTTPException(
            status_code=400,
            detail="No se pudo detectar la red local; indicá un rango, por ejemplo 192.168.1.0/24",
        )

    try:
        return await network_scan.scan(cidr)
    except network_scan.ScanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
