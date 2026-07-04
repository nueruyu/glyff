from __future__ import annotations

import asyncio
import contextvars

from glyff import Transaction

from glyff_sqlite._sqlite_client import SQLiteClient


class _ClientTransaction(Transaction):
    def __init__(self, client: SQLiteClient) -> None:
        self._client = client
        self._closed = False
        self._lock = asyncio.Lock()
        self._token: contextvars.Token | None = None
        self._staging = None

    async def begin(self) -> _ClientTransaction:
        self._token, self._staging = self._client.begin_staging()
        return self

    async def commit(self) -> None:
        async with self._lock:
            if self._closed:
                return

            if self._staging is None:
                raise RuntimeError("transaction not started")
            self._client._require_current_staging(self._staging)
            self._closed = True

            try:
                await self._client.commit_staged()
            finally:
                if self._token is None:
                    raise RuntimeError("transaction not started")
                self._client.end_staging(self._token)

    async def rollback(self) -> None:
        async with self._lock:
            if self._closed:
                return

            if self._staging is None:
                raise RuntimeError("transaction not started")
            self._client._require_current_staging(self._staging)
            self._closed = True

            try:
                await self._client.clear_staged()
            finally:
                if self._token is None:
                    raise RuntimeError("transaction not started")
                self._client.end_staging(self._token)
