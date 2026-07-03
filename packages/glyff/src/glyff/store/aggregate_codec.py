"""Shared JSON codec for the Execution aggregate.

Text backends (file, sqlite) persist an execution as a JSON object with the
same shape. This module owns that shape so the backends don't duplicate it.
Values are base64-encoded, so arbitrary (binary) serializer output round-trips.
"""

from __future__ import annotations

import base64
from typing import Any

from .._models import Execution, ExecutionId, ExecutionStatus, Metadata, SerializedValue

_STATUS_NAMES = {
    ExecutionStatus.STARTED: "started",
    ExecutionStatus.COMPLETED: "completed",
}
_NAME_TO_STATUS = {name: status for status, name in _STATUS_NAMES.items()}


def _pack_value(value: SerializedValue | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(value.data).decode("ascii")


def _unpack_value(value: object) -> SerializedValue | None:
    if not isinstance(value, str):
        return None
    return SerializedValue(base64.b64decode(value.encode("ascii")))


def _pack_metadata(metadata: dict[str, Metadata]) -> dict[str, str]:
    return {
        key: base64.b64encode(item.value.data).decode("ascii")
        for key, item in metadata.items()
    }


def _unpack_metadata(raw: object) -> dict[str, Metadata]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Metadata] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            result[key] = Metadata(key=key, value=SerializedValue(_b64_decode(value)))
    return result


def _b64_decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def execution_to_dict(execution: Execution) -> dict[str, Any]:
    """Serialize an Execution aggregate to a JSON-ready dict."""
    return {
        "status": _STATUS_NAMES[execution.status],
        "result_b64": _pack_value(execution.result),
        "metadata": _pack_metadata(execution.metadata),
    }


def execution_from_dict(execution_id: ExecutionId, stored: dict[str, Any]) -> Execution:
    """Rebuild an Execution aggregate from a JSON dict produced above."""
    return Execution(
        id=execution_id,
        status=_NAME_TO_STATUS[stored["status"]],
        result=_unpack_value(stored.get("result_b64")),
        metadata=_unpack_metadata(stored.get("metadata")),
    )
