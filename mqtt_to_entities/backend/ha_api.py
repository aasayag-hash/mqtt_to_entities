from __future__ import annotations

import logging
import os
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


def set_state(entity_id: str, state: Any, attributes: dict[str, Any] | None = None) -> bool:
    url = f"{SUPERVISOR_API_BASE}/states/{entity_id}"
    body = {"state": state, "attributes": attributes or {}}
    try:
        response = httpx.post(url, json=body, headers=_headers(), timeout=5.0)
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("Failed to update %s: %s", entity_id, exc)
        return False
