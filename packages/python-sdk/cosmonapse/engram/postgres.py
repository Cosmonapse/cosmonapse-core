"""
cosmonapse.engram.postgres
~~~~~~~~~~~~~~~~~~~~~~~~~~
Postgres Engram via asyncpg. JSONB content + GIN indexes for tags.

asyncpg is lazy-imported so this module loads without the dep  -  only
connect() raises a clear ImportError when the package is missing.

Recall surface matches SqliteEngram for portability:

    query   = {"text": str?, "tag": str?, "merge_key": str?, "top_k": int = 50}
    filters = {"tags": list[str]?, "since": iso?, "until": iso?}

For vector search, layer a separate PgVectorEngram on top of pgvector
rather than extending this. See ENGRAM_DESIGN.md §6.

Install:
    pip install "cosmonapse[postgres]"   # or: pip install asyncpg
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cosmonapse.engram.base import Engram, Hit, ImprintReceipt
from cosmonapse.envelope import new_engram_id

if TYPE_CHECKING:
    import asyncpg  # type: ignore[import-untyped]  # noqa: F401

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cosmonapse_engram_entries (
    id           TEXT PRIMARY KEY,
    engram_kind  TEXT NOT NULL,
    merge_key    TEXT,
    content      JSONB NOT NULL,
    tags         TEXT[] NOT NULL DEFAULT '{}',
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS cosmonapse_engram_kind_idx
    ON cosmonapse_engram_entries (engram_kind);
CREATE INDEX IF NOT EXISTS cosmonapse_engram_merge_key_idx
    ON cosmonapse_engram_entries (merge_key)
    WHERE merge_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS cosmonapse_engram_updated_idx
    ON cosmonapse_engram_entries (updated_at DESC);
CREATE INDEX IF NOT EXISTS cosmonapse_engram_tags_gin
    ON cosmonapse_engram_entries USING gin (tags);

CREATE TABLE IF NOT EXISTS cosmonapse_engram_imprint_seen (
    imprint_id TEXT PRIMARY KEY,
    entry_id   TEXT NOT NULL,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _ensure_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _row_to_entry_dict(row: Any) -> dict[str, Any]:
    content = row["content"]
    if isinstance(content, str):
        content = json.loads(content)
    tags = list(row["tags"] or [])
    meta = row["meta"] or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    out: dict[str, Any] = {
        "id": row["id"],
        "content": content,
        "tags": tags,
        "version": row["version"],
        "created_at": _ensure_dt(row["created_at"]),
        "updated_at": _ensure_dt(row["updated_at"]),
    }
    if row["merge_key"] is not None:
        out["merge_key"] = row["merge_key"]
    if meta:
        out["meta"] = meta
    return out


class PostgresEngram(Engram):
    """Postgres-backed Engram via asyncpg.

    Parameters
    ----------
    dsn          Postgres DSN (``postgresql://user:pass@host:5432/db``).
    engram_id    Address advertised in REGISTER.
    engram_kind  Routing label.
    capabilities Free-form list of supported query features.
    pool_kwargs  Extra kwargs passed straight to ``asyncpg.create_pool``.
    """

    def __init__(
        self,
        *,
        dsn: str,
        engram_id: str = "engram-postgres",
        engram_kind: str = "relational",
        capabilities: list[str] | None = None,
        version: str | None = "0.0.1",
        min_size: int = 1,
        max_size: int = 5,
        pool_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._dsn = dsn
        self.engram_id = engram_id
        self.engram_kind = engram_kind
        self.capabilities = capabilities or [
            "substring", "tags", "merge_key", "time_range", "jsonb",
        ]
        self.version = version
        self._min_size = min_size
        self._max_size = max_size
        self._pool_kwargs = pool_kwargs or {}
        self._pool: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "PostgresEngram requires asyncpg. Install with "
                "`pip install asyncpg` or `pip install \"cosmonapse[postgres]\"`."
            ) from exc

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            **self._pool_kwargs,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)

    async def close(self) -> None:
        if self._pool is None:
            return
        pool = self._pool
        self._pool = None
        await pool.close()

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------

    async def recall(
        self,
        query: dict[str, Any],
        *,
        filters: dict[str, Any] | None = None,
        context_ref: str | None = None,
        deadline_ms: int | None = None,
        min_confidence: float | None = None,
    ) -> list[Hit]:
        assert self._pool is not None, "PostgresEngram.connect() not called"

        query = query or {}
        filters = filters or {}
        text = (query.get("text") or "").lower()
        tag_q = query.get("tag")
        merge_key = query.get("merge_key")
        top_k = int(query.get("top_k", 50))
        require_tags = list(filters.get("tags") or [])
        since = filters.get("since")
        until = filters.get("until")

        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []

        def _p(value: Any) -> str:
            params.append(value)
            return f"${len(params)}"

        if merge_key is not None:
            clauses.append(f"merge_key = {_p(merge_key)}")
        if require_tags:
            clauses.append(f"tags @> {_p(require_tags)}")
        if tag_q is not None:
            clauses.append(f"{_p(tag_q)} = ANY(tags)")
        if since is not None:
            clauses.append(f"updated_at >= {_p(since)}")
        if until is not None:
            clauses.append(f"updated_at <= {_p(until)}")
        if text:
            # Cheap substring over JSONB-serialised content.
            clauses.append(f"content::text ILIKE {_p('%' + text + '%')}")

        sql = (
            "SELECT id, engram_kind, merge_key, content, tags, meta, "
            "version, created_at, updated_at, deleted_at "
            "FROM cosmonapse_engram_entries "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC "
            f"LIMIT {_p(top_k)}"
        )

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        hits: list[Hit] = []
        for row in rows:
            ent = _row_to_entry_dict(row)
            score = 1.0
            if text:
                hay = json.dumps(ent.get("content")).lower()
                score = min(1.0, len(text) / max(1, len(hay)))
            if min_confidence is not None and score < min_confidence:
                continue
            hits.append(Hit(id=ent["id"], entry=ent, score=score))
        return hits

    # ------------------------------------------------------------------
    # Imprint
    # ------------------------------------------------------------------

    async def imprint(
        self,
        op: str,
        entry: dict[str, Any],
        *,
        merge_key: str | None = None,
        imprint_id: str | None = None,
        trace_id: str | None = None,
    ) -> ImprintReceipt:
        assert self._pool is not None, "PostgresEngram.connect() not called"
        t0 = time.monotonic()

        resulting_id: str | None = None
        resulting_version: int | None = None
        error: str | None = None

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if imprint_id is not None:
                    seen = await conn.fetchrow(
                        "SELECT entry_id FROM cosmonapse_engram_imprint_seen "
                        "WHERE imprint_id = $1",
                        imprint_id,
                    )
                    if seen is not None:
                        seen_id = seen["entry_id"]
                        ver_row = await conn.fetchrow(
                            "SELECT version FROM cosmonapse_engram_entries "
                            "WHERE id = $1",
                            seen_id,
                        )
                        return ImprintReceipt(
                            engram_id=self.engram_id, op=op,
                            id=seen_id,
                            version=ver_row["version"] if ver_row else None,
                            took_ms=int((time.monotonic() - t0) * 1000),
                        )

                if op == "add":
                    eid = entry.get("id") or new_engram_id()
                    try:
                        await conn.execute(
                            "INSERT INTO cosmonapse_engram_entries "
                            "(id, engram_kind, merge_key, content, tags, meta) "
                            "VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb)",
                            eid, self.engram_kind, merge_key,
                            json.dumps(entry.get("content")),
                            list(entry.get("tags") or []),
                            json.dumps(entry.get("meta") or {}),
                        )
                        resulting_id, resulting_version = eid, 1
                    except Exception as exc:  # noqa: BLE001
                        error = f"add: {exc}"

                elif op == "append":
                    eid = new_engram_id()
                    await conn.execute(
                        "INSERT INTO cosmonapse_engram_entries "
                        "(id, engram_kind, merge_key, content, tags, meta) "
                        "VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb)",
                        eid, self.engram_kind, merge_key,
                        json.dumps(entry.get("content")),
                        list(entry.get("tags") or []),
                        json.dumps(entry.get("meta") or {}),
                    )
                    resulting_id, resulting_version = eid, 1

                elif op == "upsert":
                    if merge_key is None:
                        error = "upsert requires merge_key"
                    else:
                        existing = await conn.fetchrow(
                            "SELECT id, version FROM cosmonapse_engram_entries "
                            "WHERE merge_key = $1 AND deleted_at IS NULL "
                            "ORDER BY updated_at DESC LIMIT 1",
                            merge_key,
                        )
                        if existing is None:
                            eid = entry.get("id") or new_engram_id()
                            await conn.execute(
                                "INSERT INTO cosmonapse_engram_entries "
                                "(id, engram_kind, merge_key, content, tags, meta) "
                                "VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb)",
                                eid, self.engram_kind, merge_key,
                                json.dumps(entry.get("content")),
                                list(entry.get("tags") or []),
                                json.dumps(entry.get("meta") or {}),
                            )
                            resulting_id, resulting_version = eid, 1
                        else:
                            eid = existing["id"]
                            new_version = existing["version"] + 1
                            await conn.execute(
                                "UPDATE cosmonapse_engram_entries SET "
                                "content=$1::jsonb, tags=$2, meta=$3::jsonb, "
                                "version=$4, updated_at=now() WHERE id=$5",
                                json.dumps(entry.get("content")),
                                list(entry.get("tags") or []),
                                json.dumps(entry.get("meta") or {}),
                                new_version, eid,
                            )
                            resulting_id, resulting_version = eid, new_version

                elif op == "merge":
                    if merge_key is None:
                        error = "merge requires merge_key"
                    else:
                        existing = await conn.fetchrow(
                            "SELECT id, content, tags, meta, version "
                            "FROM cosmonapse_engram_entries "
                            "WHERE merge_key = $1 AND deleted_at IS NULL "
                            "ORDER BY updated_at DESC LIMIT 1",
                            merge_key,
                        )
                        if existing is None:
                            error = f"no entry for merge_key={merge_key!r}"
                        else:
                            from cosmonapse.engram.memory import _deep_merge as _dm
                            old_content = existing["content"]
                            if isinstance(old_content, str):
                                old_content = json.loads(old_content)
                            old_meta = existing["meta"] or {}
                            if isinstance(old_meta, str):
                                old_meta = json.loads(old_meta)
                            new_content = _dm(old_content, entry.get("content"))
                            new_tags = list({
                                *(existing["tags"] or []),
                                *(entry.get("tags") or []),
                            })
                            new_meta = _dm(old_meta, entry.get("meta")) or {}
                            new_version = existing["version"] + 1
                            await conn.execute(
                                "UPDATE cosmonapse_engram_entries SET "
                                "content=$1::jsonb, tags=$2, meta=$3::jsonb, "
                                "version=$4, updated_at=now() WHERE id=$5",
                                json.dumps(new_content),
                                new_tags,
                                json.dumps(new_meta),
                                new_version, existing["id"],
                            )
                            resulting_id = existing["id"]
                            resulting_version = new_version

                elif op == "delete":
                    target_id = entry.get("id")
                    if target_id is None and merge_key is not None:
                        row = await conn.fetchrow(
                            "SELECT id FROM cosmonapse_engram_entries "
                            "WHERE merge_key = $1 AND deleted_at IS NULL "
                            "ORDER BY updated_at DESC LIMIT 1",
                            merge_key,
                        )
                        if row is not None:
                            target_id = row["id"]
                    if target_id is not None:
                        await conn.execute(
                            "UPDATE cosmonapse_engram_entries SET "
                            "deleted_at = now() WHERE id = $1",
                            target_id,
                        )
                        resulting_id = target_id
                else:
                    error = f"unknown op {op!r}"

                if (
                    imprint_id is not None
                    and resulting_id is not None
                    and error is None
                ):
                    await conn.execute(
                        "INSERT INTO cosmonapse_engram_imprint_seen "
                        "(imprint_id, entry_id) VALUES ($1, $2) "
                        "ON CONFLICT (imprint_id) DO NOTHING",
                        imprint_id, resulting_id,
                    )

        return ImprintReceipt(
            engram_id=self.engram_id,
            op=op,
            id=resulting_id,
            version=resulting_version,
            took_ms=int((time.monotonic() - t0) * 1000),
            error=error,
        )
