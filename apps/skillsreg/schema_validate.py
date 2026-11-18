"""Structural validity + injection bounds for peer-supplied skill JSON Schemas.

`service._schema_gate` refuses invalid schemas unless
`MESHYCHAT_SKILL_SCHEMA_ENFORCE` explicitly relaxes enforcement.

`{}` and boolean schemas pass - every existing schema-less skill stays unaffected.
"""
from __future__ import annotations

import hashlib
import json

import rfc8785

#: 64 KiB bounds the injection surface BEFORE any walk.
MAX_SCHEMA_BYTES = 64 * 1024
MAX_SCHEMA_DEPTH = 32
#: JSON Schema Draft 2020-12 `type` keyword values.
VALID_TYPES = ("null", "boolean", "object", "array", "number", "string", "integer")


class SchemaValidateError(ValueError):
    """`SchemaValidateError`; its message is the text clients see."""


def _type_name(value) -> str:
    """Return the JSON type name used in validation errors."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def input_schema_digest(input_schema) -> str:
    """Lowercase-hex sha256 of the JCS-canonical schema.

    Not used by the registry write path; exported because the catalog summary shape
    (`SkillSummary.input_schema_hash`) is defined in terms of it and a second,
    disagreeing implementation elsewhere in this project would be worse than one here.
    """
    return hashlib.sha256(rfc8785.dumps(input_schema)).hexdigest()


def _check_type(value) -> None:
    """Validate a `type` value: a string, or an array of strings."""
    if isinstance(value, str):
        if value not in VALID_TYPES:
            raise SchemaValidateError(f"schema 'type' is not a valid JSON Schema type: {value}")
        return
    if isinstance(value, list):
        for item in value:
            _check_type(item)
        return
    raise SchemaValidateError(
        f"schema 'type' is not a valid JSON Schema type: {json.dumps(value)}")


def _walk(value, depth: int) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise SchemaValidateError(f"schema nesting exceeds depth {MAX_SCHEMA_DEPTH}")
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            raise SchemaValidateError(
                f"external $ref not allowed (only internal '#...' refs): {ref}")
        if "type" in value:
            _check_type(value["type"])
        for item in value.values():
            _walk(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _walk(item, depth + 1)


def _validate_one(schema) -> None:
    # (1) size bound FIRST - cheap, and it bounds the work the walk below does.
    try:
        length = len(json.dumps(schema, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        # An unserializable value is treated as over the cap rather than as a pass.
        raise SchemaValidateError("schema exceeds 65536 bytes (got unserializable)")
    if length > MAX_SCHEMA_BYTES:
        raise SchemaValidateError(f"schema exceeds {MAX_SCHEMA_BYTES} bytes (got {length})")
    # (2) top-level shape: a JSON Schema is an object or a boolean. `bool` is checked
    # BEFORE `dict` because in Python `True` is not a dict but IS an int - ordering the
    # branches the other way would misreport a boolean schema as a number.
    if not isinstance(schema, (bool, dict)):
        raise SchemaValidateError(
            f"not a JSON Schema: must be an object or boolean, got {_type_name(schema)}")
    # (3) depth + external-$ref + type sanity.
    _walk(schema, 0)


def validate_skill_schema(input_schema, output_schema=None) -> None:
    """Raise `SchemaValidateError` on invalid schemas; return None on success."""
    _validate_one(input_schema)
    if output_schema is not None:
        _validate_one(output_schema)
