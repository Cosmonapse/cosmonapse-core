"""
cosmonapse.storage.sqlite
~~~~~~~~~~~~~~~~~~~~~~~~~
Stdlib-sqlite3 RegistryStore.

Zero external dependencies, a single file on disk (or :memory:). All
DB calls are dispatched to a default-thread-pool executor so the event
loop is never blocked.

Schema is created on `connect()` if it does not already exist.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from cosmonapse.storage.base import NeuronRecord, RegistryStore


_SCHEMA = """
CREATE TABLE IF NOT EXISTS neurons (
    neuron_id      TEXT PRIMARY KEY,
    capabilities   TEXT NOT NULL DEFAULT '[]',
    version        TEXT,
    status         TEXT NOT NULL DEFAULT 'registered',
    last_heartbeat TEXT,
    registered_at  TEXT NOT NULL
);
"""


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _record_from_row(row: tuple) -> NeuronRecord:
    return NeuronRecord(
        neuron_id=row[0],
        capabilities=json.loads(row[1]) if row[1] else [],
        version=row[2],
        status=row[3],
        last_heartbeat=_parse_ts(row[4]),
        registered_at=_parse_ts(row[5]) or datetime.now(timezone.utc),
    )


class SqliteRegistryStore(RegistryStore):
    """
    SQLite-backed RegistryStore.

    Parameters
    ----------
    path  Filesystem path to the DB file. Use ":memory:" for an
          ephemeral in-process DB (useful for tests; not shared
          across connections  -  single connection only).
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._conn is not None:
            return

        def _open():
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn

        self._conn = await self._run(_open)

    async def close(self) -> None:
        if self._conn is None:
            return
        conn = self._conn
        self._conn = None
        await self._run(conn.close)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def upsert(self, record: NeuronRecord) -> None:
        assert self._conn is not None, "SqliteRegistryStore.connect() not called"

        def _write():
            cur = self._conn.cursor()
            existing = cur.execute(
                "SELECT registered_at FROM neurons WHERE neuron_id = ?",
                (record.neuron_id,),
            ).fetchone()
            registered_at = (
                existing[0] if existing is not None else record.registered_at.isoformat()
            )
            cur.execute(
                """
                INSERT INTO neurons
                    (neuron_id, capabilities, version, status,
                     last_heartbeat, registered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(neuron_id) DO UPDATE SET
                    capabilities   = excluded.capabilities,
                    version        = excluded.version,
                    status         = excluded.status,
                    last_heartbeat = excluded.last_heartbeat
                """,
                (
                    record.neuron_id,
                    json.dumps(record.capabilities),
                    record.version,
                    record.status,
                    record.last_heartbeat.isoformat() if record.last_heartbeat else None,
                    registered_at,
                ),
            )
            self._conn.commit()

        async with self._lock:
            await self._run(_write)

    async def mark_deregistered(self, neuron_id: str) -> None:
        assert self._conn is not None

        def _write():
            self._conn.execute(
                "UPDATE neurons SET status = 'deregistered' WHERE neuron_id = ?",
                (neuron_id,),
            )
            self._conn.commit()

        async with self._lock:
            await self._run(_write)

    async def touch_heartbeat(
        self,
        neuron_id: str,
        ts: datetime,
        status: str | None = None,
    ) -> None:
        assert self._conn is not None
        ts_iso = ts.isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()

        def _write():
            cur = self._conn.cursor()
            existing = cur.execute(
                "SELECT neuron_id FROM neurons WHERE neuron_id = ?", (neuron_id,)
            ).fetchone()
            if existing is None:
                cur.execute(
                    """
                    INSERT INTO neurons
                        (neuron_id, capabilities, version, status,
                         last_heartbeat, registered_at)
                    VALUES (?, '[]', NULL, ?, ?, ?)
                    """,
                    (neuron_id, status or "registered", ts_iso, now_iso),
                )
            elif status is not None:
                cur.execute(
                    "UPDATE neurons SET last_heartbeat = ?, status = ? "
                    "WHERE neuron_id = ?",
                    (ts_iso, status, neuron_id),
                )
            else:
                cur.execute(
                    "UPDATE neurons SET last_heartbeat = ? WHERE neuron_id = ?",
                    (ts_iso, neuron_id),
                )
            self._conn.commit()

        async with self._lock:
            await self._run(_write)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(self, neuron_id: str) -> NeuronRecord | None:
        assert self._conn is not None

        def _read():
            row = self._conn.execute(
                "SELECT neuron_id, capabilities, version, status, "
                "last_heartbeat, registered_at FROM neurons WHERE neuron_id = ?",
                (neuron_id,),
            ).fetchone()
            return _record_from_row(row) if row is not None else None

        return await self._run(_read)

    async def list(
        self,
        *,
        capability: str | None = None,
        include_deregistered: bool = False,
    ) -> list[NeuronRecord]:
        assert self._conn is not None

        def _read():
            sql = (
                "SELECT neuron_id, capabilities, version, status, "
                "last_heartbeat, registered_at FROM neurons"
            )
            params: list = []
            clauses: list[str] = []
            if not include_deregistered:
                clauses.append("status != 'deregistered'")
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            rows = self._conn.execute(sql, params).fetchall()
            out = [_record_from_row(r) for r in rows]
            if capability is not None:
                out = [r for r in out if capability in r.capabilities]
            return out

        return await self._run(_read)
