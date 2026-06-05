"""
cosmonapse.storage.postgres
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Postgres RegistryStore via asyncpg.

asyncpg is lazy-imported so this module loads without the dep  -  only
connect() raises a clear ImportError when the package is missing.

Schema is bootstrapped on first connect(). Use a dedicated schema /
database for the Cortex if you don't want it sharing a namespace with
your application tables.

Install:
    pip install "cosmonapse[postgres]"   # or: pip install asyncpg
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cosmonapse.storage.base import NeuronRecord, RegistryStore

if TYPE_CHECKING:
    import asyncpg  # noqa: F401

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cosmonapse_neurons (
    neuron_id      TEXT PRIMARY KEY,
    capabilities   JSONB NOT NULL DEFAULT '[]'::jsonb,
    version        TEXT,
    status         TEXT NOT NULL DEFAULT 'registered',
    last_heartbeat TIMESTAMPTZ,
    registered_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cosmonapse_neurons_status_idx
    ON cosmonapse_neurons (status);
"""


def _record_from_row(row: Any) -> NeuronRecord:
    caps = row["capabilities"]
    if isinstance(caps, str):
        # asyncpg returns JSONB as decoded already, but be defensive.
        import json as _json
        caps = _json.loads(caps)
    return NeuronRecord(
        neuron_id=row["neuron_id"],
        capabilities=list(caps or []),
        version=row["version"],
        status=row["status"],
        last_heartbeat=row["last_heartbeat"],
        registered_at=row["registered_at"],
    )


class PostgresRegistryStore(RegistryStore):
    """
    Parameters
    ----------
    dsn         Postgres DSN, e.g. "postgresql://user:pass@host:5432/db".
    pool_size   Min / max pool sizes for asyncpg.
    pool_kwargs Extra kwargs passed straight to asyncpg.create_pool.
    """

    def __init__(
        self,
        *,
        dsn: str,
        min_size: int = 1,
        max_size: int = 5,
        pool_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool_kwargs = pool_kwargs or {}
        self._pool: Any = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "PostgresRegistryStore requires 'asyncpg'. "
                "Install it with: pip install 'cosmonapse[postgres]'  "
                "(or: pip install asyncpg)"
            ) from exc

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            **self._pool_kwargs,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        logger.info("PostgresRegistryStore connected (pool %d-%d)",
                    self._min_size, self._max_size)

    async def close(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    async def upsert(self, record: NeuronRecord) -> None:
        assert self._pool is not None, "PostgresRegistryStore.connect() not called"
        import json as _json
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO cosmonapse_neurons
                    (neuron_id, capabilities, version, status,
                     last_heartbeat, registered_at)
                VALUES ($1, $2::jsonb, $3, $4, $5, $6)
                ON CONFLICT (neuron_id) DO UPDATE SET
                    capabilities   = EXCLUDED.capabilities,
                    version        = EXCLUDED.version,
                    status         = EXCLUDED.status,
                    last_heartbeat = EXCLUDED.last_heartbeat
                """,
                record.neuron_id,
                _json.dumps(record.capabilities),
                record.version,
                record.status,
                record.last_heartbeat,
                record.registered_at,
            )

    async def mark_deregistered(self, neuron_id: str) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE cosmonapse_neurons SET status = 'deregistered' "
                "WHERE neuron_id = $1",
                neuron_id,
            )

    async def touch_heartbeat(
        self,
        neuron_id: str,
        ts: datetime,
        status: str | None = None,
    ) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            if status is not None:
                await conn.execute(
                    """
                    INSERT INTO cosmonapse_neurons
                        (neuron_id, last_heartbeat, status)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (neuron_id) DO UPDATE SET
                        last_heartbeat = EXCLUDED.last_heartbeat,
                        status         = EXCLUDED.status
                    """,
                    neuron_id, ts, status,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO cosmonapse_neurons (neuron_id, last_heartbeat)
                    VALUES ($1, $2)
                    ON CONFLICT (neuron_id) DO UPDATE SET
                        last_heartbeat = EXCLUDED.last_heartbeat
                    """,
                    neuron_id, ts,
                )

    async def get(self, neuron_id: str) -> NeuronRecord | None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT neuron_id, capabilities, version, status, "
                "last_heartbeat, registered_at "
                "FROM cosmonapse_neurons WHERE neuron_id = $1",
                neuron_id,
            )
            return _record_from_row(row) if row is not None else None

    async def list(
        self,
        *,
        capability: str | None = None,
        include_deregistered: bool = False,
    ) -> list[NeuronRecord]:
        assert self._pool is not None

        clauses: list[str] = []
        params: list[Any] = []

        if not include_deregistered:
            clauses.append("status <> 'deregistered'")
        if capability is not None:
            params.append(capability)
            clauses.append(f"capabilities @> to_jsonb(${len(params)}::text)")

        sql = (
            "SELECT neuron_id, capabilities, version, status, "
            "last_heartbeat, registered_at FROM cosmonapse_neurons"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [_record_from_row(r) for r in rows]
