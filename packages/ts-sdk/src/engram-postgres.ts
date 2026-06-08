/**
 * @cosmonapse/sdk  -  Postgres Engram
 *
 * Ported from `cosmonapse.engram.postgres`. JSONB content + GIN-indexed tags,
 * backed by `pg` (node-postgres; lazy-imported, optional dependency:
 * `npm i pg`). The Python port uses asyncpg; `pg` is the de-facto Node driver.
 *
 * Recall surface matches SqliteEngram for portability:
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
CREATE INDEX IF NOT EXISTS cosmonapse_engram_kind_idx ON cosmonapse_engram_entries (engram_kind);
CREATE INDEX IF NOT EXISTS cosmonapse_engram_merge_key_idx ON cosmonapse_engram_entries (merge_key) WHERE merge_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS cosmonapse_engram_updated_idx ON cosmonapse_engram_entries (updated_at DESC);
CREATE INDEX IF NOT EXISTS cosmonapse_engram_tags_gin ON cosmonapse_engram_entries USING gin (tags);
CREATE TABLE IF NOT EXISTS cosmonapse_engram_imprint_seen (
    imprint_id TEXT PRIMARY KEY,
    entry_id   TEXT NOT NULL,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
`;

// Minimal structural types for the slice of node-postgres we use.
interface PgResult {
  rows: Array<Record<string, unknown>>;
}
interface PgClient {
  query(text: string, params?: unknown[]): Promise<PgResult>;
  release(): void;
}
interface PgPool {
  query(text: string, params?: unknown[]): Promise<PgResult>;
  connect(): Promise<PgClient>;
  end(): Promise<void>;
}
interface PgPoolConfig {
  connectionString: string;
  min?: number;
  max?: number;
}
interface PgModule {
  Pool?: new (config: PgPoolConfig) => PgPool;
  default?: { Pool: new (config: PgPoolConfig) => PgPool };
}

function asStringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

function asJson(v: unknown, fallback: unknown): unknown {
  if (typeof v === "string") {
    try {
      return JSON.parse(v);
    } catch {
      return fallback;
    }
  }
  return v ?? fallback;
}

function toIso(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (v instanceof Date) return v.toISOString();
  return String(v);
}

function rowToEntryDict(row: Record<string, unknown>): Json {
  const out: Json = {
    id: String(row["id"]),
    content: asJson(row["content"], null),
    tags: asStringArray(row["tags"]),
    version: typeof row["version"] === "number" ? row["version"] : Number(row["version"]),
    created_at: toIso(row["created_at"]),
    updated_at: toIso(row["updated_at"]),
  };
  if (row["merge_key"] !== null && row["merge_key"] !== undefined) out["merge_key"] = row["merge_key"];
  const meta = asJson(row["meta"], {});
  if (meta !== null && typeof meta === "object" && Object.keys(meta as Json).length > 0) {
    out["meta"] = meta;
  }
  return out;
}

export interface PostgresEngramInit {
  dsn: string;
  engramId?: string;
  engramKind?: string;
  capabilities?: string[];
  version?: string | null;
  minSize?: number;
  maxSize?: number;
}

/** Postgres-backed Engram (optional `pg`). */
export class PostgresEngram extends Engram {
  engramId: string;
  engramKind: string;
  capabilities: string[];

  private readonly dsn: string;
  private readonly minSize: number;
  private readonly maxSize: number;
  private pool: PgPool | null = null;

  constructor(init: PostgresEngramInit) {
    super();
    this.dsn = init.dsn;
    this.engramId = init.engramId ?? "engram-postgres";
    this.engramKind = init.engramKind ?? "relational";
    this.capabilities = init.capabilities ?? ["substring", "tags", "merge_key", "time_range", "jsonb"];
    this.version = init.version ?? "0.0.1";
    this.minSize = init.minSize ?? 1;
    this.maxSize = init.maxSize ?? 5;
  }

  async connect(): Promise<void> {
    if (this.pool !== null) return;
    const specifier = "pg";
    let mod: PgModule;
    try {
      mod = (await import(specifier)) as unknown as PgModule;
    } catch (err) {
      throw new Error(
        "PostgresEngram requires the 'pg' package. Install it with: npm i pg" +
          (err instanceof Error ? ` (${err.message})` : ""),
      );
    }
    const Pool = mod.Pool ?? mod.default?.Pool;
    if (Pool === undefined) throw new Error("PostgresEngram: could not load Pool from 'pg'");
    this.pool = new Pool({ connectionString: this.dsn, min: this.minSize, max: this.maxSize });
    await this.pool.query(SCHEMA);
  }

  async close(): Promise<void> {
    if (this.pool === null) return;
    const pool = this.pool;
    this.pool = null;
    await pool.end();
  }

  private require(): PgPool {
    if (this.pool === null) throw new Error("PostgresEngram.connect() not called");
    return this.pool;
  }

  async recall(query: Json, opts: RecallOptions = {}): Promise<Hit[]> {
    const pool = this.require();
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
    const p = (value: unknown): string => {
      params.push(value);
      return `$${params.length}`;
    };
    if (mergeKey !== null) clauses.push(`merge_key = ${p(mergeKey)}`);
    if (requireTags.length > 0) clauses.push(`tags @> ${p(requireTags)}`);
    if (tagQ !== null) clauses.push(`${p(tagQ)} = ANY(tags)`);
    if (since !== null) clauses.push(`updated_at >= ${p(since)}`);
    if (until !== null) clauses.push(`updated_at <= ${p(until)}`);
    if (text) clauses.push(`content::text ILIKE ${p(`%${text}%`)}`);

    const sql =
      "SELECT id, engram_kind, merge_key, content, tags, meta, version, created_at, updated_at, deleted_at " +
      `FROM cosmonapse_engram_entries WHERE ${clauses.join(" AND ")} ORDER BY updated_at DESC LIMIT ${p(topK)}`;

    const res = await pool.query(sql, params);
    const hits: Hit[] = [];
    for (const row of res.rows) {
      const ent = rowToEntryDict(row);
      let score = 1.0;
      if (text) {
        const hay = JSON.stringify(ent["content"]).toLowerCase();
        score = Math.min(1.0, text.length / Math.max(1, hay.length));
      }
      if (opts.minConfidence !== undefined && score < opts.minConfidence) continue;
      hits.push({ id: String(row["id"]), entry: ent, score });
    }
    return hits;
  }

  async imprint(op: ImprintOp, entry: Json, opts: ImprintOptions = {}): Promise<ImprintReceipt> {
    const pool = this.require();
    const t0 = Date.now();
    const mergeKey = opts.mergeKey ?? null;
    const tookMs = (): number => Date.now() - t0;
    const contentJson = JSON.stringify(entry["content"] ?? null);
    const tags = asStringArray(entry["tags"]);
    const metaJson = JSON.stringify(entry["meta"] ?? {});

    let resultingId: string | null = null;
    let version: number | null = null;
    let error: string | null = null;

    const client = await pool.connect();
    try {
      await client.query("BEGIN");

      if (opts.imprintId !== undefined) {
        const seen = await client.query(
          "SELECT entry_id FROM cosmonapse_engram_imprint_seen WHERE imprint_id = $1",
          [opts.imprintId],
        );
        const seenRow = seen.rows[0];
        if (seenRow !== undefined) {
          const seenId = String(seenRow["entry_id"]);
          const ver = await client.query("SELECT version FROM cosmonapse_engram_entries WHERE id = $1", [seenId]);
          const verRow = ver.rows[0];
          await client.query("COMMIT");
          return receipt(this.engramId, op, {
            id: seenId,
            version: verRow !== undefined ? Number(verRow["version"]) : null,
            tookMs: tookMs(),
          });
        }
      }

      const insert = async (id: string): Promise<void> => {
        await client.query(
          "INSERT INTO cosmonapse_engram_entries (id, engram_kind, merge_key, content, tags, meta) " +
            "VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb)",
          [id, this.engramKind, mergeKey, contentJson, tags, metaJson],
        );
      };

      if (op === "add") {
        const id = typeof entry["id"] === "string" ? (entry["id"] as string) : newEngramId();
        try {
          await insert(id);
          resultingId = id;
          version = 1;
        } catch (err) {
          error = `add: ${err instanceof Error ? err.message : String(err)}`;
        }
      } else if (op === "append") {
        const id = newEngramId();
        await insert(id);
        resultingId = id;
        version = 1;
      } else if (op === "upsert") {
        if (mergeKey === null) {
          error = "upsert requires merge_key";
        } else {
          const existing = await client.query(
            "SELECT id, version FROM cosmonapse_engram_entries WHERE merge_key = $1 AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1",
            [mergeKey],
          );
          const row = existing.rows[0];
          if (row === undefined) {
            const id = typeof entry["id"] === "string" ? (entry["id"] as string) : newEngramId();
            await insert(id);
            resultingId = id;
            version = 1;
          } else {
            const id = String(row["id"]);
            version = Number(row["version"]) + 1;
            await client.query(
              "UPDATE cosmonapse_engram_entries SET content=$1::jsonb, tags=$2, meta=$3::jsonb, version=$4, updated_at=now() WHERE id=$5",
              [contentJson, tags, metaJson, version, id],
            );
            resultingId = id;
          }
        }
      } else if (op === "merge") {
        if (mergeKey === null) {
          error = "merge requires merge_key";
        } else {
          const existing = await client.query(
            "SELECT id, content, tags, meta, version FROM cosmonapse_engram_entries WHERE merge_key = $1 AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1",
            [mergeKey],
          );
          const row = existing.rows[0];
          if (row === undefined) {
            error = `no entry for merge_key='${mergeKey}'`;
          } else {
            const newContent = deepMerge(asJson(row["content"], null), entry["content"]);
            const newTags = [...new Set([...asStringArray(row["tags"]), ...tags])];
            const newMeta = deepMerge(asJson(row["meta"], {}), entry["meta"]);
            version = Number(row["version"]) + 1;
            await client.query(
              "UPDATE cosmonapse_engram_entries SET content=$1::jsonb, tags=$2, meta=$3::jsonb, version=$4, updated_at=now() WHERE id=$5",
              [JSON.stringify(newContent), newTags, JSON.stringify(newMeta ?? {}), version, String(row["id"])],
            );
            resultingId = String(row["id"]);
          }
        }
      } else if (op === "delete") {
        let targetId: string | null = typeof entry["id"] === "string" ? (entry["id"] as string) : null;
        if (targetId === null && mergeKey !== null) {
          const row = await client.query(
            "SELECT id FROM cosmonapse_engram_entries WHERE merge_key = $1 AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1",
            [mergeKey],
          );
          const r = row.rows[0];
          if (r !== undefined) targetId = String(r["id"]);
        }
        if (targetId !== null) {
          await client.query("UPDATE cosmonapse_engram_entries SET deleted_at = now() WHERE id = $1", [targetId]);
          resultingId = targetId;
        }
      } else {
        error = `unknown op '${op as string}'`;
      }

      if (opts.imprintId !== undefined && resultingId !== null && error === null) {
        await client.query(
          "INSERT INTO cosmonapse_engram_imprint_seen (imprint_id, entry_id) VALUES ($1,$2) ON CONFLICT (imprint_id) DO NOTHING",
          [opts.imprintId, resultingId],
        );
      }

      await client.query("COMMIT");
    } catch (err) {
      await client.query("ROLLBACK");
      error = error ?? (err instanceof Error ? err.message : String(err));
    } finally {
      client.release();
    }

    return receipt(this.engramId, op, { id: resultingId, version, error, tookMs: tookMs() });
  }
}
