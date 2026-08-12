from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("mqtt_to_entities.mappings_store")

DATA_DIR = Path("/data")
MAPPINGS_FILE = DATA_DIR / "mappings.json"

_lock = threading.RLock()

# In-memory copy of the file. Every MQTT message needs the mapping list, and the
# add-on subscribes to "#", so re-reading the file per message meant hundreds of
# reads per second. The file is only read on first access; writes update both.
_cache: list[dict[str, Any]] | None = None

# How often last_value/last_error may hit the disk, at most.
RUNTIME_FLUSH_SECONDS = 10.0
_last_flush = 0.0


def _atomic_write(data: list[dict[str, Any]]) -> None:
    """Write via temp file + os.replace so a crash can't truncate the store.

    A plain write_text() opens with "w" (truncate) and then writes, so being
    killed mid-write left a 0-byte or partial file that broke the add-on
    permanently. os.replace is atomic on the same filesystem.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2)

    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".mappings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, MAPPINGS_FILE)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_from_disk() -> list[dict[str, Any]]:
    """Read the store, tolerating a missing or corrupted file.

    A corrupted file used to raise straight out of every endpoint, leaving the
    add-on returning 500s forever. Recovering to an empty list would silently
    drop the user's mappings, so the bad file is preserved for inspection.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not MAPPINGS_FILE.exists():
        return []

    try:
        raw = MAPPINGS_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("No se pudo leer %s: %s", MAPPINGS_FILE, exc)
        return []

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        backup = MAPPINGS_FILE.with_suffix(".corrupt")
        logger.error(
            "%s está corrupto (%s). Se preserva una copia en %s y se arranca vacío.",
            MAPPINGS_FILE,
            exc,
            backup,
        )
        try:
            os.replace(MAPPINGS_FILE, backup)
        except OSError:
            pass
        return []

    if not isinstance(data, list):
        logger.error("%s no contiene una lista; se ignora.", MAPPINGS_FILE)
        return []
    return data


def _get_cache() -> list[dict[str, Any]]:
    global _cache
    if _cache is None:
        _cache = _load_from_disk()
    return _cache


def _persist() -> None:
    _atomic_write(_get_cache())


def list_mappings() -> list[dict[str, Any]]:
    with _lock:
        # Copy so callers iterating the result can't be tripped up by a
        # concurrent write, and can't mutate the cache by accident.
        return [dict(m) for m in _get_cache()]


def mappings_for_topic(topic: str, broker_id: str | None = None) -> list[dict[str, Any]]:
    """Mappings matching a topic, for the hot MQTT path.

    Called for every message on every topic (the add-on subscribes to "#"), so
    it avoids copying the whole store the way list_mappings does and filters in
    one pass instead.
    """
    with _lock:
        matches = []
        for mapping in _get_cache():
            if mapping.get("topic") != topic:
                continue
            # Mappings are bound to one broker; legacy rows without broker_id
            # match any broker so they keep working until edited.
            mapping_broker = mapping.get("broker_id")
            if broker_id and mapping_broker and mapping_broker != broker_id:
                continue
            matches.append(dict(mapping))
        return matches


def get_mapping(mapping_id: str) -> dict[str, Any] | None:
    with _lock:
        for mapping in _get_cache():
            if mapping["id"] == mapping_id:
                return dict(mapping)
    return None


def create_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        mappings = _get_cache()
        mapping = dict(mapping)
        mapping["id"] = str(uuid.uuid4())
        mapping.setdefault("last_value", None)
        mapping.setdefault("last_error", None)
        mappings.append(mapping)
        _persist()
        return dict(mapping)


def update_mapping(mapping_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        for mapping in _get_cache():
            if mapping["id"] == mapping_id:
                mapping.update(updates)
                _persist()
                return dict(mapping)
        return None


def delete_mapping(mapping_id: str) -> bool:
    global _cache
    with _lock:
        mappings = _get_cache()
        remaining = [m for m in mappings if m["id"] != mapping_id]
        if len(remaining) == len(mappings):
            return False
        _cache = remaining
        _persist()
        return True


def _touch_runtime_state(mapping_id: str, updates: dict[str, Any]) -> None:
    """Update live state in memory, flushing to disk at most once per interval.

    last_value/last_error change on every matching MQTT message. Persisting each
    one rewrote the whole file, which on a busy Victron broker meant continuous
    writes to the Pi's SD card. These fields are only a convenience (the UI reads
    them, and startup re-pushes them), so losing the last few seconds of them on
    a hard kill is acceptable; the mapping definitions themselves still persist
    immediately via create/update/delete.
    """
    global _last_flush
    with _lock:
        found = False
        for mapping in _get_cache():
            if mapping["id"] == mapping_id:
                mapping.update(updates)
                found = True
                break
        if not found:
            return

        now = time.monotonic()
        if now - _last_flush >= RUNTIME_FLUSH_SECONDS:
            _last_flush = now
            _persist()


def flush() -> None:
    """Force pending runtime state to disk (used on shutdown)."""
    global _last_flush
    with _lock:
        _last_flush = time.monotonic()
        _persist()


def set_last_value(mapping_id: str, value: Any) -> None:
    # A successful push clears any previous error so the UI stops showing a
    # stale failure once the mapping starts working. last_update_at is what the
    # staleness watchdog compares against.
    _touch_runtime_state(
        mapping_id,
        {"last_value": value, "last_error": None, "last_update_at": time.time()},
    )


def set_last_error(mapping_id: str, error: str | None) -> None:
    _touch_runtime_state(mapping_id, {"last_error": error})


def set_unknown(mapping_id: str, unknown_state: str) -> None:
    """Blank a mapping's value without restarting its staleness clock."""
    _touch_runtime_state(mapping_id, {"last_value": unknown_state, "last_error": None})


def set_last_update_at(mapping_id: str, when: float) -> None:
    _touch_runtime_state(mapping_id, {"last_update_at": when})
