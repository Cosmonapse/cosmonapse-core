"""
cosmonapse.engram.sqlite
~~~~~~~~~~~~~~~~~~~~~~~~
Stdlib-sqlite3 Engram. Zero deps; single-file DB or ``:memory:``.

All DB calls dispatch to the default thread-pool executor so the event
loop is never blocked. Schema created on first connect().

Recall surface matches InMemoryEngram:

    query = {"text": str?, "tag": str?, "merge_key": str?, "top_k": int = 50}
    filters = {"tags": list[str]?, "since": iso?, "until": iso?}

For richer semantics (vector search, BM25), implement a separate
backend rather than extending this one.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from cosmonapse.engram.base import Engram, Hit, ImprintReceipt
from cosmonapse.envelope import new_engram_id


_SCHEMA = """
CREATE TABLE IF NOT EXISTS engram_entries (
    id           TEXT PRIMARY KEY,
    engram_kind  TEXT NOT NULL,
    merge_key    TEXT,
    content      TEXT NOT NULL,         -- JSON
    tags         TEXT NOT NULL DEFAULT '[]',  -- JSON array
    meta         TEXT NOT NULL DEFAULT '{}',  -- JSON
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    deleted_at   TEXT
);

CREATE INDEX IF NOT EXISTS engram_entries_kind_idx
    ON engram_entries (engram_kind);
CREATE INDEX IF NOT EXISTS engram_entries_merge_key_idx
    ON engram_entries (merge_key)
    WHERE merge_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS engram_entries_updated_idx
    ON engram_entries (updated_at);

CREATE TABLE IF NOT EXISTS engram_imprint_seen (
    imprint_id TEXT PRIMARY KEY,
    entry_id   TEXT NOT NULL,
    seen_at    TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_entry_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    (eid, kind, merge_key, content, tags, meta, version,
     created_at, updated_at, _deleted_at) = row
    out: dict[str, Any] = {
        "id": eid,
        "content": json.loads(content),
        "tags": json.loads(tags) if tags else [],
        "version": version,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    if merge_key is not None:
        out["merge_key"] = merge_key
    parsed_meta = json.loads(meta) if meta else {}
    if parsed_meta:
        out["meta"] = parsed_meta
    return out


class SqliteEngram(Engram):
    """SQLite-backed Engram.

    Parameters
    ----------
    path         Path to the DB file, or ``":memory:"`` for an
                 ephemeral in-process DB (single connection).
    engram_id    Address advertised in REGISTER. Default
                 ``"engram-sqlite"``; pass an explicit value when
                 hosting more than one in a namespace.
    engram_kind  Routing label.
    capabilities Free-form list of supported query features.
    """

    def __init__(
        self,
        *,
        path: str = ":memory:",
        engram_id: str = "engram-sqlite",
        engram_kind: str = "relational",
        capabilities: list[str] | None = None,
        version: str | None = "0.0.1",
    ) -> None:
        self._path = path
        self.engram_id = engram_id
        self.engram_kind = engram_kind
        self.capabilities = capabilities or [
            "substring", "tags", "merge_key", "time_range",
        ]
        self.version = version

        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def _run(self, fn: Any, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._conn is not None:
            return

        def _open() -> sqlite3.Connection:
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
        assert self._conn is not None, "SqliteEngram.connect() not called"
        conn = self._conn

        query = query or {}
        filters = filters or {}
        text = (query.get("text") or "").lower()
        tag_q = query.get("tag")
        merge_key = query.get("merge_key")
        top_k = int(query.get("top_k", 50))
        require_tags = list(filters.get("tags") or [])
        since = filters.get("since")
        until = filters.get("until")

        def _read() -> list[Any]:
            sql_parts = [
                "SELECT id, engram_kind, merge_key, content, tags, meta, "
                "version, created_at, updated_at, deleted_at "
                "FROM engram_entries WHERE deleted_at IS NULL"
            ]
            params: list[Any] = []
            if merge_key is not None:
                sql_parts.append("AND merge_key = ?")
                params.append(merge_key)
            if since is not None:
                sql_parts.append("AND updated_at >= ?")
                params.append(since)
            if until is not None:
                sql_parts.append("AND updated_at <= ?")
                params.append(until)
            sql = " ".join(sql_parts) + " ORDER BY updated_at DESC"
            rows = conn.execute(sql, params).fetchall()
            return rows

        rows = await self._run(_read)

        hits: list[Hit] = []
        for row in rows:
            ent = _row_to_entry_dict(row)
            tags = ent.get("tags", [])
            if require_tags and not set(require_tags).issubset(set(tags)):
                continue
            if tag_q is not None and tag_q not in tags:
                continue
            score = 1.0
            if text:
                hay = json.dumps(ent.get("content")).lower()
                if text not in hay:
                    continue
                score = min(1.0, len(text) / max(1, len(hay)))
            if min_confidence is not None and score < min_confidence:
                continue
            hits.append(Hit(id=ent["id"], entry=ent, score=score))
            if len(hits) >= top_k:
                break
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
    ) -> ImprintReceipt:
        assert self._conn is not None, "SqliteEngram.connect() not called"
        conn = self._conn
        t0 = time.monotonic()

        async with self._lock:

            def _check_seen() -> str | None:
                if imprint_id is None:
                    return None
                row = conn.execute(
                    "SELECT entry_id FROM engram_imprint_seen "
                    "WHERE imprint_id = ?",
                    (imprint_id,),
                ).fetchone()
                return row[0] if row else None

            seen_entry_id = await self._run(_check_seen)
            if seen_entry_id is not None:
                def _read_version() -> int | None:
                    row = conn.execute(
                        "SELECT version FROM engram_entries WHERE id = ?",
                        (seen_entry_id,),
                    ).fetchone()
                    return row[0] if row else None
                ver = await self._run(_read_version)
                return ImprintReceipt(
                    engram_id=self.engram_id, op=op,
                    id=seen_entry_id, version=ver,
                    took_ms=int((time.monotonic() - t0) * 1000),
                )

            resulting_id: str | None = None
            resulting_version: int | None = None
            error: str | None = None

            if op == "add":
                eid = entry.get("id") or new_engram_id()

                def _insert() -> None:
                    conn.execute(
                        "INSERT INTO engram_entries "
                        "(id, engram_kind, merge_key, content, tags, meta, "
                        " version, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            eid, self.engram_kind, merge_key,
                            json.dumps(entry.get("content")),
                            json.dumps(entry.get("tags") or []),
                            json.dumps(entry.get("meta") or {}),
                            1, _now_iso(), _now_iso(),
                        ),
                    )
                    conn.commit()

                try:
                    await self._run(_insert)
                    resulting_id = eid
                    resulting_version = 1
                except sqlite3.IntegrityError as exc:
                    error = f"add: id collision ({exc})"

            elif op == "append":
                # Append generates a fresh id every time.
                eid = new_engram_id()

                def _insert() -> None:
                    conn.execute(
                        "INSERT INTO engram_entries "
                        "(id, engram_kind, merge_key, content, tags, meta, "
                        " version, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            eid, self.engram_kind, merge_key,
                            json.dumps(entry.get("content")),
                            json.dumps(entry.get("tags") or []),
                            json.dumps(entry.get("meta") or {}),
                            1, _now_iso(), _now_iso(),
                        ),
                    )
                    conn.commit()

                await self._run(_insert)
                resulting_id = eid
                resulting_version = 1

            elif op == "upsert":

                def _upsert() -> tuple[tuple[str, int] | None, str | None]:
                    if merge_key is None:
                        return None, "upsert requires merge_key"
                    existing = conn.execute(
                        "SELECT id, version, created_at FROM engram_entries "
                        "WHERE merge_key = ? AND deleted_at IS NULL "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (merge_key,),
                    ).fetchone()
                    if existing is None:
                        eid = entry.get("id") or new_engram_id()
                        conn.execute(
                            "INSERT INTO engram_entries "
                            "(id, engram_kind, merge_key, content, tags, meta,"
                            " version, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (
                                eid, self.engram_kind, merge_key,
                                json.dumps(entry.get("content")),
                                json.dumps(entry.get("tags") or []),
                                json.dumps(entry.get("meta") or {}),
                                1, _now_iso(), _now_iso(),
                            ),
                        )
                        conn.commit()
                        return (eid, 1), None
                    eid, old_version, created_at = existing
                    new_version = old_version + 1
                    conn.execute(
                        "UPDATE engram_entries SET "
                        "content = ?, tags = ?, meta = ?, version = ?, "
                        "updated_at = ? WHERE id = ?",
                        (
                            json.dumps(entry.get("content")),
                            json.dumps(entry.get("tags") or []),
                            json.dumps(entry.get("meta") or {}),
                            new_version,
                            _now_iso(),
                            eid,
                        ),
                    )
                    conn.commit()
                    return (eid, new_version), None

                outcome, err_msg = await self._run(_upsert)
                if err_msg is not None:
                    error = err_msg
                else:
                    resulting_id, resulting_version = outcome

            elif op == "merge":

                def _merge() -> tuple[tuple[str, int] | None, str | None]:
                    if merge_key is None:
                        return None, "merge requires merge_key"
                    existing = conn.execute(
                        "SELECT id, content, tags, meta, version "
                        "FROM engram_entries "
                        "WHERE merge_key = ? AND deleted_at IS NULL "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (merge_key,),
                    ).fetchone()
                    if existing is None:
                        return None, f"no entry for merge_key={merge_key!r}"
                    eid, old_content, old_tags, old_meta, old_version = existing
                    from cosmonapse.engram.memory import _deep_merge as _dm
                    new_content = _dm(
                        json.loads(old_content), entry.get("content"),
                    )
                    new_tags = list({
                        *(json.loads(old_tags) if old_tags else []),
                        *(entry.get("tags") or []),
                    })
                    new_meta = _dm(
                        json.loads(old_meta) if old_meta else {},
                        entry.get("meta"),
                    ) or {}
                    new_version = old_version + 1
                    conn.execute(
                        "UPDATE engram_entries SET "
                        "content = ?, tags = ?, meta = ?, version = ?, "
                        "updated_at = ? WHERE id = ?",
                        (
                            json.dumps(new_content),
                            json.dumps(new_tags),
                            json.dumps(new_meta),
                            new_version,
                            _now_iso(),
                            eid,
                        ),
                    )
                    conn.commit()
                    return (eid, new_version), None

                outcome, err_msg = await self._run(_merge)
                if err_msg is not None:
                    error = err_msg
                else:
                    resulting_id, resulting_version = outcome

            elif op == "delete":

                def _delete() -> str | None:
                    target_id = entry.get("id")
                    if target_id is None and merge_key is not None:
                        row = conn.execute(
                            "SELECT id FROM engram_entries "
                            "WHERE merge_key = ? AND deleted_at IS NULL "
                            "ORDER BY updated_at DESC LIMIT 1",
                            (merge_key,),
                        ).fetchone()
                        if row is None:
                            return None
                        target_id = row[0]
                    if target_id is None:
                        return None
                    conn.execute(
                        "UPDATE engram_entries SET deleted_at = ? "
                        "WHERE id = ?",
                        (_now_iso(), target_id),
                    )
                    conn.commit()
                    return target_id

                resulting_id = await self._run(_delete)
                resulting_version = None

            else:
                error = f"unknown op {op!r}"

            if imprint_id is not None and resulting_id is not None and error is None:
                def _record_seen() -> None:
                    conn.execute(
                        "INSERT OR IGNORE INTO engram_imprint_seen "
                        "(imprint_id, entry_id, seen_at) VALUES (?,?,?)",
                        (imprint_id, resulting_id, _now_iso()),
                    )
                    conn.commit()
                await self._run(_record_seen)

        return ImprintReceipt(
            engram_id=self.engram_id,
            op=op,
            id=resulting_id,
            version=resulting_version,
            took_ms=int((time.monotonic() - t0) * 1000),
            error=error,
        )
