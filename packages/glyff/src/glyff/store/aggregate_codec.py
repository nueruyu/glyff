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
from ..serialization._utils import stable_json_dumps
from ..serialization.constants import DEFAULT_ENCODING

_STATUS_NAMES = {
    ExecutionStatus.STARTED: "started",
    ExecutionStatus.COMPLETED: "completed",
}
_NAME_TO_STATUS = {name: status for status, name in _STATUS_NAMES.items()}


def _pack_value(value: SerializedValue | None) -> Any | None:
    if value is None:
        return None
    return json.loads(value.data.decode(DEFAULT_ENCODING))


def _unpack_value(value: object) -> SerializedValue | None:
    return SerializedValue(_json_bytes(value))


def _pack_metadata(metadata: dict[str, Metadata]) -> dict[str, Any]:
    return {key: _pack_value(item.value) for key, item in metadata.items()}


def _unpack_metadata(raw: object) -> dict[str, Metadata]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Metadata] = {}
    for key, value in raw.items():
        if isinstance(key, str):
            result[key] = Metadata(key=key, value=SerializedValue(_json_bytes(value)))
    return result


def _json_bytes(value: object) -> bytes:
    return stable_json_dumps(value, ensure_ascii=False).encode(DEFAULT_ENCODING)


def execution_to_dict(execution: Execution) -> dict[str, Any]:
    """Serialize an Execution aggregate to a JSON-ready dict."""
    return {
        "status": _STATUS_NAMES[execution.status],
        "result": _pack_value(execution.result),
        "metadata": _pack_metadata(execution.metadata),
    }


def execution_from_dict(execution_id: ExecutionId, stored: dict[str, Any]) -> Execution:
    """Rebuild an Execution aggregate from a JSON dict produced above."""
    status = _NAME_TO_STATUS[stored["status"]]
    return Execution(
        id=execution_id,
        status=status,
        result=_unpack_value(stored.get("result"))
        if status is ExecutionStatus.COMPLETED
        else None,
        metadata=_unpack_metadata(stored.get("metadata")),
    )
