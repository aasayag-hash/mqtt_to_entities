from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("mqtt_to_entities.brokers_store")

DATA_DIR = Path("/data")
BROKERS_FILE = DATA_DIR / "brokers.json"

_lock = threading.Lock()


def _atomic_write(data: list[dict[str, Any]]) -> None:
    """Write via temp file + os.replace so a crash can't truncate the store.

    write_text() opens with "w" (truncate) and then writes, so being killed
    mid-write left a partial file. list_brokers() then recovers to [] and the
    next save consolidates the loss -- the user's hosts, usernames and
    passwords are gone and every mapping is left orphaned. Same approach as
    mappings_store._atomic_write.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2)

    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".brokers-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, BROKERS_FILE)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def list_brokers() -> list[dict[str, Any]]:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not BROKERS_FILE.exists():
            return []

        try:
            raw = BROKERS_FILE.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("No se pudo leer %s: %s", BROKERS_FILE, exc)
            return []

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            # Preserve the bad file instead of letting the next save overwrite
            # it: it still holds the hosts and credentials the user typed in.
            backup = BROKERS_FILE.with_suffix(".corrupt")
            logger.error(
                "%s está corrupto (%s). Se preserva una copia en %s y se arranca sin brokers.",
                BROKERS_FILE,
                exc,
                backup,
            )
            try:
                os.replace(BROKERS_FILE, backup)
            except OSError:
                pass
            return []

        if not isinstance(data, list):
            logger.error("%s no contiene una lista; se ignora.", BROKERS_FILE)
            return []
        return data


def save_brokers(brokers: list[dict[str, Any]]) -> None:
    with _lock:
        _atomic_write(brokers)
