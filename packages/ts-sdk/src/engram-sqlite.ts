/**
 * @cosmonapse/sdk  -  SQLite Engram
 *
 * Ported from `cosmonapse.engram.sqlite`. A single file on disk (or
 * `:memory:`), backed by `better-sqlite3` (lazy-imported, optional dependency:
 * `npm i better-sqlite3`). The Python port uses stdlib sqlite3 on a threadpool;
 * better-sqlite3 is synchronous, so the async `Engram` methods simply wrap
 * synchronous calls.
 *
 * Recall surface matches InMemoryEngram:
 *   query   = { text?, tag?, merge_key?, top_k = 50 }
 *   filters = { tags?: string[], since?: iso, until?: iso }
 */

import { newEngramId, type Json } from "./envelope.js";
import {
  Engram,
  deepMerge,
  receipt,
  type Hit,
  type ImprintOp,
  type ImprintOptions,
  type ImprintReceipt,
  type RecallOptions,
} from "./engram.js";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS engram_entries (
    id           TEXT PRIMARY KEY,
    engram_kind  TEXT NOT NULL,
    merge_key    TEXT,
    content      TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    meta         TEXT NOT NULL DEFAULT '{}',
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    deleted_at   TEXT
);
CREATE INDEX IF NOT EXISTS engram_entries_kind_idx ON engram_entries (engram_kind);
CREATE INDEX IF NOT EXISTS engram_entries_merge_key_idx ON engram_entries (merge_key) WHERE merge_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS engram_entries_updated_idx ON engram_entries (updated_at);
CREATE TABLE IF NOT EXISTS engram_imprint_seen (
    imprint_id TEXT PRIMARY KEY,
    entry_id   TEXT NOT NULL,
    seen_at    TEXT NOT NULL
);
`;

// Minimal structural types for the slice of better-sqlite3 we use.
interface SqliteStatement {
  get(...params: unknown[]): unknown;
  all(...params: unknown[]): unknown[];
  run(...params: unknown[]): unknown;
}
interface SqliteDatabase {
  exec(sql: string): void;
  prepare(sql: string): SqliteStatement;
  close(): void;
}
type SqliteModule = { default: new (path: string) => SqliteDatabase };

interface EntryRow {
  id: string;
  engram_kind: string;
  merge_key: string | null;
  content: string;
  tags: string | null;
  meta: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

function nowIso(): string {
  return new Date().toISOString();
}

function parseJson(s: string | null, fallback: unknown): unknown {
  if (!s) return fallback;
  try {
    return JSON.parse(s);
  } catch {
    return fallback;
  }
}

function rowToEntryDict(row: EntryRow): Json {
  const out: Json = {
    id: row.id,
    content: parseJson(row.content, null),
    tags: parseJson(row.tags, []),
    version: row.version,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
  if (row.merge_key !== null) out["merge_key"] = row.merge_key;
  const meta = parseJson(row.meta, {});
  if (meta !== null && typeof meta === "object" && Object.keys(meta as Json).length > 0) {
    out["meta"] = meta;
  }
  return out;
}

function asStringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

export interface SqliteEngramInit {
  path?: string;
  engramId?: string;
  engramKind?: string;
  capabilities?: string[];
  version?: string | null;
}

/** SQLite-backed Engram (optional `better-sqlite3`). */
export class SqliteEngram extends Engram {
  engramId: string;
  engramKind: string;
  capabilities: string[];

  private readonly path: string;
  private db: SqliteDatabase | null = null;

  constructor(init: SqliteEngramInit = {}) {
    super();
    this.path = init.path ?? ":memory:";
    this.engramId = init.engramId ?? "engram-sqlite";
    this.engramKind = init.engramKind ?? "relational";
    this.capabilities = init.capabilities ?? ["substring", "tags", "merge_key", "time_range"];
    this.version = init.version ?? "0.0.1";
  }

  async connect(): Promise<void> {
    if (this.db !== null) return;
    const specifier = "better-sqlite3";
    let mod: SqliteModule;
    try {
      mod = (await import(specifier)) as unknown as SqliteModule;
    } catch (err) {
      throw new Error(
        "SqliteEngram requires the 'better-sqlite3' package. Install it with: npm i better-sqlite3" +
          (err instanceof Error ? ` (${err.message})` : ""),
      );
    }
    const Database = mod.default;
    this.db = new Database(this.path);
    this.db.exec(SCHEMA);
  }

  async close(): Promise<void> {
    if (this.db === null) return;
    this.db.close();
    this.db = null;
  }

  private require(): SqliteDatabase {
    if (this.db === null) throw new Error("SqliteEngram.connect() not called");
    return this.db;
  }

  async recall(query: Json, opts: RecallOptions = {}): Promise<Hit[]> {
    const db = this.require();
    const q = query ?? {};
    const text = typeof q["text"] === "string" ? (q["text"] as string).toLowerCase() : "";
    const tagQ = typeof q["tag"] === "string" ? (q["tag"] as string) : null;
    const mergeKey = typeof q["merge_key"] === "string" ? (q["merge_key"] as string) : null;
    const topK = typeof q["top_k"] === "number" ? (q["top_k"] as number) : 50;
    const filters = opts.filters ?? {};
    const requireTags = asStringArray(filters["tags"]);
    const since = typeof filters["since"] === "string" ? (filters["since"] as string) : null;
    const until = typeof filters["until"] === "string" ? (filters["until"] as string) : null;

    const clauses = ["deleted_at IS NULL"];
    const params: unknown[] = [];
    if (mergeKey !== null) {
      clauses.push("merge_key = ?");
      params.push(mergeKey);
    }
    if (since !== null) {
      clauses.push("updated_at >= ?");
      params.push(since);
    }
    if (until !== null) {
      clauses.push("updated_at <= ?");
      params.push(until);
    }
    const sql =
      "SELECT id, engram_kind, merge_key, content, tags, meta, version, created_at, updated_at, deleted_at " +
      `FROM engram_entries WHERE ${clauses.join(" AND ")} ORDER BY updated_at DESC`;
    const rows = db.prepare(sql).all(...params) as EntryRow[];

    const hits: Hit[] = [];
    for (const row of rows) {
      const ent = rowToEntryDict(row);
      const tags = asStringArray(ent["tags"]);
      if (requireTags.length > 0 && !requireTags.every((t) => tags.includes(t))) continue;
      if (tagQ !== null && !tags.includes(tagQ)) continue;
      let score = 1.0;
      if (text) {
        const hay = JSON.stringify(ent["content"]).toLowerCase();
        if (!hay.includes(text)) continue;
        score = Math.min(1.0, text.length / Math.max(1, hay.length));
      }
      if (opts.minConfidence !== undefined && score < opts.minConfidence) continue;
      hits.push({ id: row.id, entry: ent, score });
      if (hits.length >= topK) break;
    }
    return hits;
  }

  async imprint(op: ImprintOp, entry: Json, opts: ImprintOptions = {}): Promise<ImprintReceipt> {
    const db = this.require();
    const t0 = Date.now();
    const mergeKey = opts.mergeKey ?? null;
    const tookMs = (): number => Date.now() - t0;

    if (opts.imprintId !== undefined) {
      const seen = db
        .prepare("SELECT entry_id FROM engram_imprint_seen WHERE imprint_id = ?")
        .get(opts.imprintId) as { entry_id: string } | undefined;
      if (seen !== undefined) {
        const verRow = db
          .prepare("SELECT version FROM engram_entries WHERE id = ?")
          .get(seen.entry_id) as { version: number } | undefined;
        return receipt(this.engramId, op, {
          id: seen.entry_id,
          version: verRow ? verRow.version : null,
          tookMs: tookMs(),
        });
      }
    }

    const insert = (id: string): void => {
      db.prepare(
        "INSERT INTO engram_entries (id, engram_kind, merge_key, content, tags, meta, version, created_at, updated_at) " +
          "VALUES (?,?,?,?,?,?,?,?,?)",
      ).run(
        id,
        this.engramKind,
        mergeKey,
        JSON.stringify(entry["content"] ?? null),
        JSON.stringify(asStringArray(entry["tags"])),
        JSON.stringify(entry["meta"] ?? {}),
        1,
        nowIso(),
        nowIso(),
      );
    };

    let resultingId: string | null = null;
    let version: number | null = null;
    let error: string | null = null;

    if (op === "add") {
      const id = typeof entry["id"] === "string" ? (entry["id"] as string) : newEngramId();
      try {
        insert(id);
        resultingId = id;
        version = 1;
      } catch (err) {
        error = `add: id collision (${err instanceof Error ? err.message : String(err)})`;
      }
    } else if (op === "append") {
      const id = newEngramId();
      insert(id);
      resultingId = id;
      version = 1;
    } else if (op === "upsert") {
      if (mergeKey === null) {
        error = "upsert requires merge_key";
      } else {
        const existing = db
          .prepare(
            "SELECT id, version FROM engram_entries WHERE merge_key = ? AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1",
          )
          .get(mergeKey) as { id: string; version: number } | undefined;
        if (existing === undefined) {
          const id = typeof entry["id"] === "string" ? (entry["id"] as string) : newEngramId();
          insert(id);
          resultingId = id;
          version = 1;
        } else {
          version = existing.version + 1;
          db.prepare(
            "UPDATE engram_entries SET content = ?, tags = ?, meta = ?, version = ?, updated_at = ? WHERE id = ?",
          ).run(
            JSON.stringify(entry["content"] ?? null),
            JSON.stringify(asStringArray(entry["tags"])),
            JSON.stringify(entry["meta"] ?? {}),
            version,
            nowIso(),
            existing.id,
          );
          resultingId = existing.id;
        }
      }
    } else if (op === "merge") {
      if (mergeKey === null) {
        error = "merge requires merge_key";
      } else {
        const existing = db
          .prepare(
            "SELECT id, content, tags, meta, version FROM engram_entries WHERE merge_key = ? AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1",
          )
          .get(mergeKey) as
          | { id: string; content: string; tags: string | null; meta: string | null; version: number }
          | undefined;
        if (existing === undefined) {
          error = `no entry for merge_key='${mergeKey}'`;
        } else {
          const newContent = deepMerge(parseJson(existing.content, null), entry["content"]);
          const newTags = [...new Set([...asStringArray(parseJson(existing.tags, [])), ...asStringArray(entry["tags"])])];
          const mergedMeta = deepMerge(parseJson(existing.meta, {}), entry["meta"]);
          version = existing.version + 1;
          db.prepare(
            "UPDATE engram_entries SET content = ?, tags = ?, meta = ?, version = ?, updated_at = ? WHERE id = ?",
          ).run(
            JSON.stringify(newContent),
            JSON.stringify(newTags),
            JSON.stringify(mergedMeta ?? {}),
            version,
            nowIso(),
            existing.id,
          );
          resultingId = existing.id;
        }
      }
    } else if (op === "delete") {
      let targetId: string | null = typeof entry["id"] === "string" ? (entry["id"] as string) : null;
      if (targetId === null && mergeKey !== null) {
        const row = db
          .prepare(
            "SELECT id FROM engram_entries WHERE merge_key = ? AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1",
          )
          .get(mergeKey) as { id: string } | undefined;
        if (row !== undefined) targetId = row.id;
      }
      if (targetId !== null) {
        db.prepare("UPDATE engram_entries SET deleted_at = ? WHERE id = ?").run(nowIso(), targetId);
        resultingId = targetId;
      }
    } else {
      error = `unknown op '${op as string}'`;
    }

    if (opts.imprintId !== undefined && resultingId !== null && error === null) {
      db.prepare(
        "INSERT OR IGNORE INTO engram_imprint_seen (imprint_id, entry_id, seen_at) VALUES (?,?,?)",
      ).run(opts.imprintId, resultingId, nowIso());
    }

    return receipt(this.engramId, op, { id: resultingId, version, error, tookMs: tookMs() });
  }
}
