from __future__ import annotations

import re
from typing import Any


def flatten_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        if not value:
            if prefix:
                paths.append(prefix)
        for key, sub_value in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(flatten_paths(sub_value, child_prefix))
    elif isinstance(value, list):
        if not value:
            if prefix:
                paths.append(prefix)
        for index, item in enumerate(value):
            id_field = _identifying_field(item)
            if id_field is not None:
                field_name, field_value = id_field
                child_prefix = f"{prefix}[{field_name}={field_value}]"
            else:
                child_prefix = f"{prefix}[{index}]"
            paths.extend(flatten_paths(item, child_prefix))
    else:
        if prefix:
            paths.append(prefix)
    return paths


def _identifying_field(item: Any) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    for candidate in ("id", "ID", "Id", "name", "uid"):
        if candidate in item and isinstance(item[candidate], (str, int, float)):
            return candidate, str(item[candidate])
    return None


_SEGMENT_RE = re.compile(
    r"""
    ^
    (?P<key>[^.\[\]]*)
    (?:\[(?P<index>[^=\]]+)\])?
    (?:\[(?P<field>[^=\]]+)=(?P<match>[^\]]+)\])?
    $
    """,
    re.VERBOSE,
)


def resolve_path(payload: Any, path: str) -> Any:
    if not path:
        return payload

    current = payload
    for segment in _split_path(path):
        current = _resolve_segment(current, segment)
        if current is _MISSING:
            return None
    return current


_MISSING = object()


def _split_path(path: str) -> list[str]:
    tokens: list[str] = []
    buf = ""
    depth = 0
    for ch in path:
        if ch == "." and depth == 0:
            tokens.append(buf)
            buf = ""
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        buf += ch
    if buf:
        tokens.append(buf)
    return tokens


def _resolve_segment(current: Any, segment: str) -> Any:
    match = _SEGMENT_RE.match(segment)
    if not match:
        return _MISSING

    key = match.group("key")
    index = match.group("index")
    field = match.group("field")
    field_match = match.group("match")

    if key:
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]

    if index is not None:
        if not isinstance(current, list):
            return _MISSING
        try:
            idx = int(index)
        except ValueError:
            return _MISSING
        if idx < 0 or idx >= len(current):
            return _MISSING
        current = current[idx]

    if field is not None:
        if not isinstance(current, list):
            return _MISSING
        found = _MISSING
        for item in current:
            if isinstance(item, dict) and str(item.get(field)) == field_match:
                found = item
                break
        current = found

    return current
