from __future__ import annotations

from typing import Any


_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}
_SCHEMA_KEYS = {"additionalProperties", "description", "properties", "required", "title", "type"}
_PROPERTY_SCHEMA_KEYS = {"description", "title", "type"}


def validate_arguments_against_schema(
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> None:
    """Validate the supported, deliberately small MCP input-schema subset.

    Unknown keywords and malformed schema values fail closed. This is not a
    general JSON Schema implementation; registries needing additional
    constraints must wait until those constraints are explicitly supported.
    """
    if not isinstance(arguments, dict) or not isinstance(schema, dict):
        raise ValueError("registered tool input_schema and arguments must be objects")

    unsupported = sorted(set(schema) - _SCHEMA_KEYS)
    if unsupported:
        raise ValueError(f"registered tool input_schema uses unsupported keyword: {unsupported[0]}")
    if schema.get("type", "object") != "object":
        raise ValueError("registered tool input_schema must be an object schema")

    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise ValueError("registered tool input_schema required must be a list of strings")
    if len(set(required)) != len(required):
        raise ValueError("registered tool input_schema required entries must be unique")

    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or any(not isinstance(name, str) for name in properties):
        raise ValueError("registered tool input_schema properties must be an object")
    missing_definitions = sorted(set(required) - set(properties))
    if missing_definitions:
        raise ValueError(
            "registered tool input_schema required field has no property definition: "
            f"{missing_definitions[0]}"
        )

    additional_properties = schema.get("additionalProperties", True)
    if not isinstance(additional_properties, bool):
        raise ValueError("registered tool input_schema additionalProperties must be a boolean")
    if additional_properties is False:
        extra = sorted(set(arguments) - set(properties))
        if extra:
            raise ValueError(f"tools/call arguments include unsupported field: {extra[0]}")

    for field_name, property_schema in properties.items():
        if not isinstance(property_schema, dict):
            raise ValueError(f"registered tool input_schema property is invalid: {field_name}")
        unsupported_property = sorted(set(property_schema) - _PROPERTY_SCHEMA_KEYS)
        if unsupported_property:
            raise ValueError(
                "registered tool input_schema property uses unsupported keyword: "
                f"{field_name}.{unsupported_property[0]}"
            )
        expected_type = property_schema.get("type")
        if expected_type is None:
            raise ValueError(f"registered tool input_schema property has no type: {field_name}")
        expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
        if (
            not expected_types
            or any(not isinstance(item, str) or item not in _JSON_TYPES for item in expected_types)
        ):
            raise ValueError(f"registered tool input_schema property type is unsupported: {field_name}")

    for field_name in required:
        if field_name not in arguments:
            raise ValueError(f"tools/call arguments missing required field: {field_name}")
    for field_name, value in arguments.items():
        property_schema = properties.get(field_name)
        if property_schema is None:
            continue
        expected_type = property_schema["type"]
        if not _value_matches_json_type(value, expected_type):
            type_label = "|".join(expected_type) if isinstance(expected_type, list) else expected_type
            raise ValueError(
                f"tools/call argument has wrong type: {field_name} must be {type_label}"
            )


def _value_matches_json_type(value: Any, expected_type: str | list[str]) -> bool:
    expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
    for item in expected_types:
        if item == "string" and isinstance(value, str):
            return True
        if item == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if item == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if item == "boolean" and isinstance(value, bool):
            return True
        if item == "object" and isinstance(value, dict):
            return True
        if item == "array" and isinstance(value, list):
            return True
        if item == "null" and value is None:
            return True
    return False
