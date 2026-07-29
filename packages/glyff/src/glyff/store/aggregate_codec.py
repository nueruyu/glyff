"""Shared JSON codec for the Execution aggregate.

Text backends (file, sqlite) persist an execution as a JSON object with the
same shape. This module owns that shape so the backends don't duplicate it.
Serialized values are stored as embedded JSON values, so text backends require
serializer output to be JSON text.
"""

from __future__ import annotations

import json
from typing import Any

from .._models import Execution, ExecutionId, ExecutionStatus, Metadata, SerializedValue
from ..exceptions import SerializationError
from ..serialization._utils import stable_json_dumps
from ..serialization.constants import DEFAULT_ENCODING

_STATUS_NAMES = {
    ExecutionStatus.STARTED: "started",
    ExecutionStatus.COMPLETED: "completed",
}
_NAME_TO_STATUS = {name: status for status, name in _STATUS_NAMES.items()}

_TEXT_BACKEND_JSON_ERROR = (
    "Text execution backends require SerializedValue.data to contain UTF-8 "
    "encoded JSON text. Use a JSON serializer such as JsonSerializer or "
    "PydanticSerializer."
)


def validate_json_text_value(value: SerializedValue, *, context: str = "value") -> Any:
    """Return the decoded JSON value required by text execution backends."""
    try:
        data = value.data.decode(DEFAULT_ENCODING)
    except UnicodeDecodeError as exc:
        raise SerializationError(
            f"{_TEXT_BACKEND_JSON_ERROR} Invalid {context}: data is not valid UTF-8."
        ) from exc

    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise SerializationError(
            f"{_TEXT_BACKEND_JSON_ERROR} Invalid {context}: data is not valid JSON."
        ) from exc


def _pack_value(value: SerializedValue | None, *, context: str) -> Any | None:
    if value is None:
        return None
    return validate_json_text_value(value, context=context)


def _unpack_value(value: object) -> SerializedValue | None:
    return SerializedValue(_json_bytes(value))


def _pack_metadata(metadata: dict[str, Metadata]) -> dict[str, Any]:
    return {
        key: _pack_value(item.value, context=f"metadata key {key!r}")
        for key, item in metadata.items()
    }


def _unpack_metadata(raw: object) -> dict[str, Metadata]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Metadata] = {}
    for key, value in raw.items():
        if isinstance(key, str):
            result[key] = Metadata(key=key, value=SerializedValue(_json_bytes(value)))
    return result


def _json_bytes(value: object) -> bytes:
    return stable_json_dumps(value).encode(DEFAULT_ENCODING)


def _pack_args(args: SerializedValue) -> str:
    # An opaque string, not an embedded JSON value: preserve the exact digest
    # preimage (see Execution.args).
    try:
        return args.data.decode(DEFAULT_ENCODING)
    except UnicodeDecodeError as exc:
        raise SerializationError(
            f"{_TEXT_BACKEND_JSON_ERROR} Invalid args: data is not valid UTF-8."
        ) from exc


def execution_to_dict(execution: Execution) -> dict[str, Any]:
    """Serialize an Execution aggregate to a JSON-ready dict."""
    return {
        "args": _pack_args(execution.args),
        "status": _STATUS_NAMES[execution.status],
        "result": _pack_value(execution.result, context="result"),
        "metadata": _pack_metadata(execution.metadata),
    }


def execution_from_dict(execution_id: ExecutionId, stored: dict[str, Any]) -> Execution:
    """Rebuild an Execution aggregate from a JSON dict produced above."""
    status = _NAME_TO_STATUS[stored["status"]]
    return Execution(
        id=execution_id,
        status=status,
        args=SerializedValue(stored["args"].encode(DEFAULT_ENCODING)),
        result=_unpack_value(stored.get("result"))
        if status is ExecutionStatus.COMPLETED
        else None,
        metadata=_unpack_metadata(stored.get("metadata")),
    )
