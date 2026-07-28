"""
cosmonapse.engram.memory
~~~~~~~~~~~~~~~~~~~~~~~~
In-process Engram. Holds entries in a dict. Default backend if none is
supplied. State resets every time the process restarts.

Use this for tests, local dev, and ephemeral Cortices. For anything
persistent, use SqliteEngram or PostgresEngram.

The recall surface is intentionally small:

    query = {"text": str?,            # case-insensitive substring on entry['content']
             "tag": str?,             # exact match anywhere in entry['tags']
             "merge_key": str?,       # exact match on stored merge_key
             "top_k": int = 50}
    filters = {"tags": list[str]?,    # all tags must be present
               "since": iso-datetime?,
               "until": iso-datetime?}

A backend with richer query semantics (vector search, BM25) plugs in
by subclassing Engram, not by extending this one.
"""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cosmonapse.engram.base import Engram, Hit, ImprintReceipt
from cosmonapse.envelope import new_engram_id


@dataclass
class _Entry:
    id: str
    content: Any
    tags: list[str] = field(default_factory=list)
    merge_key: str | None = None
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "content": self.content,
            "tags": list(self.tags),
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if self.merge_key is not None:
            out["merge_key"] = self.merge_key
        if self.extra:
            out["meta"] = dict(self.extra)
        return out


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


class InMemoryEngram(Engram):
    """Dict-backed Engram. Conformant for tests + dev.

    Parameters
    ----------
    engram_id    Stable identifier published in REGISTER and used to
                 address this Engram in RECALL/IMPRINT.
    engram_kind  Routing label. One of ``"context"``, ``"semantic"``,
                 ``"keyvalue"``, ``"relational"``, ``"blob"``, etc.
                 No semantics enforced; deployment convention.
    capabilities Free-form list (e.g. ``["substring", "tags"]``).
    """

    def __init__(
        self,
        *,
        engram_id: str = "engram-memory",
        engram_kind: str = "keyvalue",
        capabilities: list[str] | None = None,
        version: str | None = "0.0.1",
    ) -> None:
        self.engram_id = engram_id
        self.engram_kind = engram_kind
        self.capabilities = capabilities or ["substring", "tags", "merge_key"]
        self.version = version

        self._entries: dict[str, _Entry] = {}
        self._by_merge_key: dict[str, list[str]] = {}
        self._imprint_seen: dict[str, str] = {}  # imprint_id -> entry_id
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        return

    async def close(self) -> None:
        async with self._lock:
            self._entries.clear()
            self._by_merge_key.clear()
            self._imprint_seen.clear()

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
        text = (query.get("text") or "").lower() if query else ""
        tag_q = query.get("tag") if query else None
        merge_key = query.get("merge_key") if query else None
        top_k = int(query.get("top_k", 50)) if query else 50

        filters = filters or {}
        require_tags = set(filters.get("tags") or [])
        since = _parse_dt(filters.get("since"))
        until = _parse_dt(filters.get("until"))

        async with self._lock:
            if merge_key is not None:
                ids = self._by_merge_key.get(merge_key, [])
                candidates = [self._entries[i] for i in ids if i in self._entries]
            else:
                candidates = list(self._entries.values())

        hits: list[Hit] = []
        for ent in candidates:
            if require_tags and not require_tags.issubset(set(ent.tags)):
                continue
            if since and ent.updated_at < since:
                continue
            if until and ent.updated_at > until:
                continue
            if tag_q is not None and tag_q not in ent.tags:
                continue
            score = 1.0
            if text:
                hay = str(ent.content).lower()
                if text not in hay:
                    continue
                # Crude relevance proxy: shorter hay = higher score
                score = min(1.0, len(text) / max(1, len(hay)))
            if min_confidence is not None and score < min_confidence:
                continue
            hits.append(Hit(id=ent.id, entry=ent.to_dict(), score=score))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

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
        t0 = time.monotonic()

        async with self._lock:
            # Idempotency: replay returns the recorded receipt (no re-journal).
            if imprint_id is not None:
                seen_entry_id = self._imprint_seen.get(imprint_id)
                if seen_entry_id is not None:
                    existing = self._entries.get(seen_entry_id)
                    return ImprintReceipt(
                        engram_id=self.engram_id,
                        op=op,
                        id=seen_entry_id,
                        version=existing.version if existing else None,
                        took_ms=int((time.monotonic() - t0) * 1000),
                    )

            if op == "add":
                ent = self._make_entry(entry, merge_key)
                if ent.id in self._entries:
                    return ImprintReceipt(
                        engram_id=self.engram_id, op=op,
                        error=f"entry id {ent.id!r} already exists",
                        took_ms=int((time.monotonic() - t0) * 1000),
                    )
                self._store(ent)
                resulting_id = ent.id
                version = ent.version
                # inverse: remove what we just created
                self._saga_record(trace_id, "delete", {"id": ent.id})

            elif op == "append":
                ent = self._make_entry(entry, merge_key)
                # Append is "add but never collide"  -  autogenerate id if missing.
                while ent.id in self._entries:
                    ent = self._make_entry({**entry, "id": new_engram_id()}, merge_key)
                self._store(ent)
                resulting_id = ent.id
                version = ent.version
                self._saga_record(trace_id, "delete", {"id": ent.id})

            elif op == "upsert":
                # merge_key is required; locate existing by merge_key.
                existing_ids = self._by_merge_key.get(merge_key or "", [])
                if existing_ids:
                    target_id = existing_ids[-1]
                    old = self._entries[target_id]
                    # inverse: restore the prior content for this merge_key
                    self._saga_record(
                        trace_id, "upsert",
                        {"id": target_id, "content": copy.deepcopy(old.content),
                         "tags": list(old.tags), "meta": copy.deepcopy(old.extra)},
                        merge_key=old.merge_key,
                    )
                    new = self._make_entry({**entry, "id": target_id}, merge_key)
                    new.created_at = old.created_at
                    new.version = old.version + 1
                    self._store(new, replace=True)
                    resulting_id = new.id
                    version = new.version
                else:
                    ent = self._make_entry(entry, merge_key)
                    self._store(ent)
                    resulting_id = ent.id
                    version = ent.version
                    # inverse: the upsert created a fresh entry  -  delete it
                    self._saga_record(trace_id, "delete", {"id": ent.id})

            elif op == "merge":
                existing_ids = self._by_merge_key.get(merge_key or "", [])
                if not existing_ids:
                    return ImprintReceipt(
                        engram_id=self.engram_id, op=op,
                        error=f"no entry for merge_key={merge_key!r}",
                        took_ms=int((time.monotonic() - t0) * 1000),
                    )
                target_id = existing_ids[-1]
                old = self._entries[target_id]
                # inverse: restore prior content (merge is non-destructive of
                # the key, so an upsert back to the old value reverses it)
                self._saga_record(
                    trace_id, "upsert",
                    {"id": target_id, "content": copy.deepcopy(old.content),
                     "tags": list(old.tags), "meta": copy.deepcopy(old.extra)},
                    merge_key=old.merge_key,
                )
                merged_content = _deep_merge(old.content, entry.get("content"))
                merged_tags = list({*old.tags, *(entry.get("tags") or [])})
                merged_extra = _deep_merge(old.extra, entry.get("meta"))
                new = _Entry(
                    id=target_id,
                    content=merged_content,
                    tags=merged_tags,
                    merge_key=old.merge_key,
                    version=old.version + 1,
                    created_at=old.created_at,
                    updated_at=datetime.now(UTC),
                    extra=merged_extra or {},
                )
                self._store(new, replace=True)
                resulting_id = new.id
                version = new.version

            elif op == "delete":
                target_id = None
                if entry.get("id"):
                    target_id = entry["id"]
                elif merge_key:
                    ids = self._by_merge_key.get(merge_key, [])
                    if ids:
                        target_id = ids[-1]
                if target_id is None or target_id not in self._entries:
                    return ImprintReceipt(
                        engram_id=self.engram_id, op=op,
                        took_ms=int((time.monotonic() - t0) * 1000),
                    )
                old = self._entries[target_id]
                # inverse: re-create the deleted entry verbatim
                self._saga_record(
                    trace_id, "add",
                    {"id": old.id, "content": copy.deepcopy(old.content),
                     "tags": list(old.tags), "meta": copy.deepcopy(old.extra)},
                    merge_key=old.merge_key,
                )
                self._evict(target_id)
                resulting_id = target_id
                version = None

            else:
                # Should be caught upstream by imprint_signal, but be defensive.
                return ImprintReceipt(
                    engram_id=self.engram_id, op=op,
                    error=f"unknown op {op!r}",
                    took_ms=int((time.monotonic() - t0) * 1000),
                )

            if imprint_id is not None:
                self._imprint_seen[imprint_id] = resulting_id

        return ImprintReceipt(
            engram_id=self.engram_id,
            op=op,
            id=resulting_id,
            version=version,
            took_ms=int((time.monotonic() - t0) * 1000),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_entry(self, entry: dict[str, Any], merge_key: str | None) -> _Entry:
        eid = entry.get("id") or new_engram_id()
        content = entry.get("content")
        tags = list(entry.get("tags") or [])
        meta = dict(entry.get("meta") or {})
        return _Entry(
            id=eid,
            content=content,
            tags=tags,
            merge_key=merge_key,
            extra=meta,
        )

    def _store(self, ent: _Entry, *, replace: bool = False) -> None:
        if replace:
            old = self._entries.get(ent.id)
            if old is not None and old.merge_key:
                bucket = self._by_merge_key.get(old.merge_key, [])
                if ent.id in bucket:
                    bucket.remove(ent.id)
                if not bucket:
                    self._by_merge_key.pop(old.merge_key, None)
        self._entries[ent.id] = ent
        if ent.merge_key:
            self._by_merge_key.setdefault(ent.merge_key, []).append(ent.id)

    def _evict(self, entry_id: str) -> None:
        ent = self._entries.pop(entry_id, None)
        if ent is None:
            return
        if ent.merge_key:
            bucket = self._by_merge_key.get(ent.merge_key, [])
            if entry_id in bucket:
                bucket.remove(entry_id)
            if not bucket:
                self._by_merge_key.pop(ent.merge_key, None)

    # Test/debug helper - NOT part of the Engram ABC.
    def _snapshot(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._entries.values()]


def _deep_merge(base: Any, incoming: Any) -> Any:
    """Conservative deep merge for dicts. Lists concat-dedup. Scalars overwrite.

    Used by op=merge. Engrams with richer merge semantics override
    imprint() directly rather than tweaking this.
    """
    if incoming is None:
        return base
    if isinstance(base, dict) and isinstance(incoming, dict):
        out = dict(base)
        for k, v in incoming.items():
            if k in out:
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out
    if isinstance(base, list) and isinstance(incoming, list):
        seen = set()
        out_list: list[Any] = []
        for item in [*base, *incoming]:
            key = repr(item)
            if key in seen:
                continue
            seen.add(key)
            out_list.append(item)
        return out_list
    return incoming
