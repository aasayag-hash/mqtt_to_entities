from __future__ import annotations

from typing import Any


class TransformError(Exception):
    pass


def transform(domain: str, config: dict[str, Any], raw_value: Any) -> tuple[Any, dict[str, Any]]:
    if domain == "sensor":
        return _transform_sensor(config, raw_value)
    if domain == "binary_sensor":
        return _transform_binary_sensor(config, raw_value)
    if domain == "switch":
        return _transform_switch(config, raw_value)
    if domain == "number":
        return _transform_number(config, raw_value)
    if domain == "text":
        return _transform_text(config, raw_value)
    if domain == "select":
        return _transform_select(config, raw_value)
    raise TransformError(f"Unsupported domain: {domain}")


def _apply_precision(config: dict[str, Any], raw_value: Any) -> Any:
    """Round a numeric value to the configured number of decimals.

    MQTT floats often arrive as 57.560001373291016; "precision" trims that to
    57.56 (2) or 58 (0). Non-numeric values pass through untouched so a text
    payload on a sensor is not turned into an error.
    """
    precision = config.get("precision")
    if precision is None:
        return raw_value

    try:
        number = float(raw_value)
    except (TypeError, ValueError):
        return raw_value

    try:
        digits = int(precision)
    except (TypeError, ValueError):
        return raw_value

    if digits < 0:
        return raw_value

    rounded = round(number, digits)
    # 0 decimals should read as "58", not "58.0".
    if digits == 0:
        return int(rounded)
    return rounded


def _transform_sensor(config: dict[str, Any], raw_value: Any) -> tuple[Any, dict[str, Any]]:
    attributes: dict[str, Any] = {}
    unit = config.get("unit_of_measurement")
    if unit:
        attributes["unit_of_measurement"] = unit
    return _apply_precision(config, raw_value), attributes


def _match_on_off(config: dict[str, Any], raw_value: Any, on_state: str, off_state: str) -> tuple[str, dict[str, Any]]:
    on_values = config.get("on_values", [])
    off_values = config.get("off_values", [])
    value_str = str(raw_value)
    if value_str in [str(v) for v in on_values]:
        return on_state, {}
    if value_str in [str(v) for v in off_values]:
        return off_state, {}
    raise TransformError(f"Value {raw_value!r} does not match on_values or off_values")


def _transform_binary_sensor(config: dict[str, Any], raw_value: Any) -> tuple[Any, dict[str, Any]]:
    return _match_on_off(config, raw_value, "on", "off")


def _transform_switch(config: dict[str, Any], raw_value: Any) -> tuple[Any, dict[str, Any]]:
    return _match_on_off(config, raw_value, "on", "off")


def _transform_number(config: dict[str, Any], raw_value: Any) -> tuple[Any, dict[str, Any]]:
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise TransformError(f"Value {raw_value!r} is not numeric") from exc

    minimum = config.get("min")
    maximum = config.get("max")
    if minimum is not None and value < minimum:
        raise TransformError(f"Value {value} below min {minimum}")
    if maximum is not None and value > maximum:
        raise TransformError(f"Value {value} above max {maximum}")

    value = _apply_precision(config, value)

    if isinstance(value, float) and value == int(value):
        value = int(value)

    attributes: dict[str, Any] = {}
    if minimum is not None:
        attributes["min"] = minimum
    if maximum is not None:
        attributes["max"] = maximum
    if config.get("step") is not None:
        attributes["step"] = config["step"]
    return value, attributes


def _transform_text(config: dict[str, Any], raw_value: Any) -> tuple[Any, dict[str, Any]]:
    return str(raw_value), {}


def _transform_select(config: dict[str, Any], raw_value: Any) -> tuple[Any, dict[str, Any]]:
    options = config.get("options", [])
    value_str = str(raw_value)
    if value_str not in [str(o) for o in options]:
        raise TransformError(f"Value {raw_value!r} not in options {options}")
    return value_str, {"options": options}
