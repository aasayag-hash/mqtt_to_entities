from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger("mqtt_to_entities.ha_api")

SUPERVISOR_API_BASE = "http://supervisor/core/api"


def _headers() -> dict[str, str]:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

# Domains this add-on can push state into via the REST API.
VALID_DOMAINS = {"sensor", "binary_sensor", "switch", "number", "text", "select"}


def validate_entity_id(entity_id: str, domain: str | None = None) -> str | None:
    """Return an error message if entity_id is not usable, else None.

    Home Assistant only accepts "<domain>.<object_id>" in lowercase with
    underscores; anything else (spaces, capitals, missing domain) is rejected
    by the API, which previously surfaced only as a silent log warning.
    """
    if not entity_id:
        return "El entity ID no puede estar vacío"
    if not ENTITY_ID_RE.match(entity_id):
        return (
            f"'{entity_id}' no es un entity ID válido. Usá el formato "
            "dominio.nombre en minúsculas y con guiones bajos (ej. sensor.voltaje)"
        )
    prefix = entity_id.split(".", 1)[0]
    if prefix not in VALID_DOMAINS:
        return f"'{prefix}' no es un dominio soportado ({', '.join(sorted(VALID_DOMAINS))})"
    if domain and prefix != domain:
        return f"El entity ID debe empezar con '{domain}.' para coincidir con el dominio elegido"
    return None


def set_state(entity_id: str, state: Any, attributes: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """Push a state to HA. Returns (ok, error_message)."""
    error = validate_entity_id(entity_id)
    if error:
        logger.warning("Refusing to update invalid entity_id %r: %s", entity_id, error)
        return False, error

    url = f"{SUPERVISOR_API_BASE}/states/{entity_id}"
    body = {"state": state, "attributes": attributes or {}}
    try:
        response = httpx.post(url, json=body, headers=_headers(), timeout=5.0)
        response.raise_for_status()
        return True, None
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code} de Home Assistant"
        if exc.response.status_code == 401:
            detail += " (token del Supervisor inválido)"
        logger.warning("Failed to update %s: %s", entity_id, exc)
        return False, detail
    except httpx.HTTPError as exc:
        logger.warning("Failed to update %s: %s", entity_id, exc)
        return False, f"No se pudo contactar a Home Assistant: {exc}"
