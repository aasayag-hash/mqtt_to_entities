from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

DATA_DIR = Path("/data")
MAPPINGS_FILE = DATA_DIR / "mappings.json"

_lock = threading.Lock()


def _ensure_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not MAPPINGS_FILE.exists():
        MAPPINGS_FILE.write_text("[]", encoding="utf-8")


def list_mappings() -> list[dict[str, Any]]:
    with _lock:
        _ensure_file()
        return json.loads(MAPPINGS_FILE.read_text(encoding="utf-8"))


def get_mapping(mapping_id: str) -> dict[str, Any] | None:
    for mapping in list_mappings():
        if mapping["id"] == mapping_id:
            return mapping
    return None


def create_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        _ensure_file()
        mappings = json.loads(MAPPINGS_FILE.read_text(encoding="utf-8"))
        mapping = dict(mapping)
        mapping["id"] = str(uuid.uuid4())
        mapping.setdefault("last_value", None)
        mappings.append(mapping)
        MAPPINGS_FILE.write_text(json.dumps(mappings, indent=2), encoding="utf-8")
        return mapping


def update_mapping(mapping_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        _ensure_file()
        mappings = json.loads(MAPPINGS_FILE.read_text(encoding="utf-8"))
        for mapping in mappings:
            if mapping["id"] == mapping_id:
                mapping.update(updates)
                MAPPINGS_FILE.write_text(json.dumps(mappings, indent=2), encoding="utf-8")
                return mapping
        return None


def delete_mapping(mapping_id: str) -> bool:
    with _lock:
        _ensure_file()
        mappings = json.loads(MAPPINGS_FILE.read_text(encoding="utf-8"))
        remaining = [m for m in mappings if m["id"] != mapping_id]
        if len(remaining) == len(mappings):
            return False
        MAPPINGS_FILE.write_text(json.dumps(remaining, indent=2), encoding="utf-8")
        return True


def set_last_value(mapping_id: str, value: Any) -> None:
    update_mapping(mapping_id, {"last_value": value})
