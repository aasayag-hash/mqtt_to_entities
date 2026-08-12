from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

DATA_DIR = Path("/data")
BROKERS_FILE = DATA_DIR / "brokers.json"

_lock = threading.Lock()


def _ensure_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not BROKERS_FILE.exists():
        BROKERS_FILE.write_text("[]", encoding="utf-8")


def list_brokers() -> list[dict[str, Any]]:
    with _lock:
        _ensure_file()
        try:
            return json.loads(BROKERS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return []


def save_brokers(brokers: list[dict[str, Any]]) -> None:
    with _lock:
        _ensure_file()
        BROKERS_FILE.write_text(json.dumps(brokers, indent=2), encoding="utf-8")
