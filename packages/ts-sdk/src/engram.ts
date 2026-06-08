/**
 * @cosmonapse/sdk  -  Engram (shared memory)
 *
 * Ported from the Python `cosmonapse.engram` package (see ENGRAM_DESIGN.md).
 * An Engram is the synapse-side participant that services RECALL / IMPRINT
 * signals. Engrams are NOT Neurons: they never produce AGENT_OUTPUT. A hosting
 * Dendrite mounts one via `dendrite.attachEngram(engram)`.
 *
 * This module is the value layer: the data types, the `Engram` contract, and
 * the default in-process `InMemoryEngram`. The SQLite / Postgres backends live
 * in `engram-sqlite.ts` / `engram-postgres.ts`; the caller-side correlation
 * table lives in `engram-client.ts`.
 */

import { newEngramId, type Directed, type Json } from "./envelope.js";

export type RecallMode = "first" | "merge" | "all";
export type ImprintOp = "add" | "append" | "merge" | "upsert" | "delete";

// ---------------------------------------------------------------------------
// Public data types
// ---------------------------------------------------------------------------

/** One search result. `score` is backend-dependent; relational backends use 1.0. */
export interface Hit {
  id: string;
  entry: Json;
  score: number;
}

/** What a recall() call returns to the caller. */
export interface RecallResult {
  hits: Hit[];
  engramIds: string[];
  truncated: boolean;
  tookMs: number | null;
}

/** What an imprint() call returns to the caller. */
export interface ImprintReceipt {
  engramId: string;
  op: string;
  id: string | null;
  version: number | null;
  tookMs: number | null;
  error: string | null;
  /** Convenience: true when `error` is null. */
  ok: boolean;
}

export interface EngramBindingInit {
  name: string;
  directedId?: string;
  directedType?: string;
  defaultDeadlineMs?: number;
  defaultRecallMode?: RecallMode;
}

/**
 * Declarative wiring of one Engram into an Axon. The Neuron addresses an Engram
 * by the stable local `name`; `directedId` / `directedType` determine how
 * RECALL/IMPRINT are routed on the wire. At least one of them must be set.
 */
export class EngramBinding {
  readonly name: string;
  readonly directedId: string | null;
  readonly directedType: string | null;
  readonly defaultDeadlineMs: number | null;
  readonly defaultRecallMode: RecallMode;

  constructor(init: EngramBindingInit) {
    this.name = init.name;
    this.directedId = init.directedId ?? null;
    this.directedType = init.directedType ?? null;
    this.defaultDeadlineMs = init.defaultDeadlineMs ?? null;
    this.defaultRecallMode = init.defaultRecallMode ?? "first";
    if (!this.directedId && !this.directedType) {
      throw new Error(
        `EngramBinding '${this.name}' requires directedId (engram_id) or directedType (engram_kind), or both`,
      );
    }
  }

  /** Build the `Directed` addressing this Engram. */
  toDirected(): Directed {
    return { id: this.directedId, type: this.directedType, capabilities: [] };
  }
}

// ---------------------------------------------------------------------------
// Exceptions
// ---------------------------------------------------------------------------

export class EngramError extends Error {
  constructor(message?: string) {
    super(message);
    this.name = new.target.name;
  }
}
/** Raised when a RECALL or IMPRINT deadline elapses with no response. */
export class EngramTimeout extends EngramError {}
/** Raised when the containing TASK terminates while a call is in flight. */
export class EngramCancelled extends EngramError {}
/** Raised when a Neuron asks for an Engram binding its Axon was not wired to. */
export class EngramNotBound extends EngramError {}
/** Raised by a backend that must shed load (surfaces as an IMPRINTED error). */
export class EngramOverloaded extends EngramError {}

// ---------------------------------------------------------------------------
// Engram contract
// ---------------------------------------------------------------------------

export interface RecallOptions {
  filters?: Json;
  contextRef?: string;
  deadlineMs?: number;
  minConfidence?: number;
}

export interface ImprintOptions {
  mergeKey?: string;
  /** Originating IMPRINT signal id; backends use it for idempotency. */
  imprintId?: string;
}

/**
 * Storage wrapper. One backend per Engram instance. Every backend implements
 * this exact interface; the test suite runs against any conforming Engram.
 */
export abstract class Engram {
  abstract engramId: string;
  abstract engramKind: string;
  abstract capabilities: string[];
  version: string | null = null;

  /** Open backend resources (DB pool, file handle, ...). */
  abstract connect(): Promise<void>;
  /** Release backend resources. */
  abstract close(): Promise<void>;

  /** Return matching entries. Empty array on a miss; never throw on a miss. */
  abstract recall(query: Json, opts?: RecallOptions): Promise<Hit[]>;

  /** Write to the backend. `op` is one of add | append | merge | upsert | delete. */
  abstract imprint(op: ImprintOp, entry: Json, opts?: ImprintOptions): Promise<ImprintReceipt>;

  /** Return false if this Engram cannot satisfy the query. Default: serve all. */
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  async canServe(_query: Json): Promise<boolean> {
    return true;
  }
}

export function receipt(
  engramId: string,
  op: string,
  fields: { id?: string | null; version?: number | null; tookMs?: number | null; error?: string | null } = {},
): ImprintReceipt {
  const error = fields.error ?? null;
  return {
    engramId,
    op,
    id: fields.id ?? null,
    version: fields.version ?? null,
    tookMs: fields.tookMs ?? null,
    error,
    ok: error === null,
  };
}

// ---------------------------------------------------------------------------
// InMemoryEngram
// ---------------------------------------------------------------------------

interface MemEntry {
  id: string;
  content: unknown;
  tags: string[];
  mergeKey: string | null;
  version: number;
  createdAt: string;
  updatedAt: string;
  extra: Json;
}

function entryToDict(e: MemEntry): Json {
  const out: Json = {
    id: e.id,
    content: e.content,
    tags: [...e.tags],
    version: e.version,
    created_at: e.createdAt,
    updated_at: e.updatedAt,
  };
  if (e.mergeKey !== null) out["merge_key"] = e.mergeKey;
  if (Object.keys(e.extra).length > 0) out["meta"] = { ...e.extra };
  return out;
}

function asStringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

function asObject(v: unknown): Json {
  return v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Json) : {};
}

export interface InMemoryEngramInit {
  engramId?: string;
  engramKind?: string;
  capabilities?: string[];
  version?: string | null;
}

/** Dict-backed Engram. The default backend for tests and local dev. */
export class InMemoryEngram extends Engram {
  engramId: string;
  engramKind: string;
  capabilities: string[];

  private entries = new Map<string, MemEntry>();
  private byMergeKey = new Map<string, string[]>();
  private imprintSeen = new Map<string, string>();

  constructor(init: InMemoryEngramInit = {}) {
    super();
    this.engramId = init.engramId ?? "engram-memory";
    this.engramKind = init.engramKind ?? "keyvalue";
    this.capabilities = init.capabilities ?? ["substring", "tags", "merge_key"];
    this.version = init.version ?? "0.0.1";
  }

  async connect(): Promise<void> {
    return;
  }

  async close(): Promise<void> {
    this.entries.clear();
    this.byMergeKey.clear();
    this.imprintSeen.clear();
  }

  async recall(query: Json, opts: RecallOptions = {}): Promise<Hit[]> {
    const q = query ?? {};
    const text = typeof q["text"] === "string" ? (q["text"] as string).toLowerCase() : "";
    const tagQ = typeof q["tag"] === "string" ? (q["tag"] as string) : null;
    const mergeKey = typeof q["merge_key"] === "string" ? (q["merge_key"] as string) : null;
    const topK = typeof q["top_k"] === "number" ? (q["top_k"] as number) : 50;

    const filters = opts.filters ?? {};
    const requireTags = asStringArray(filters["tags"]);
    const since = typeof filters["since"] === "string" ? Date.parse(filters["since"] as string) : NaN;
    const until = typeof filters["until"] === "string" ? Date.parse(filters["until"] as string) : NaN;

    let candidates: MemEntry[];
    if (mergeKey !== null) {
      const ids = this.byMergeKey.get(mergeKey) ?? [];
      candidates = ids.map((i) => this.entries.get(i)).filter((e): e is MemEntry => e !== undefined);
    } else {
      candidates = [...this.entries.values()];
    }

    const hits: Hit[] = [];
    for (const ent of candidates) {
      if (requireTags.length > 0 && !requireTags.every((t) => ent.tags.includes(t))) continue;
      const updated = Date.parse(ent.updatedAt);
      if (!Number.isNaN(since) && updated < since) continue;
      if (!Number.isNaN(until) && updated > until) continue;
      if (tagQ !== null && !ent.tags.includes(tagQ)) continue;
      let score = 1.0;
      if (text) {
        const hay = String(ent.content).toLowerCase();
        if (!hay.includes(text)) continue;
        score = Math.min(1.0, text.length / Math.max(1, hay.length));
      }
      if (opts.minConfidence !== undefined && score < opts.minConfidence) continue;
      hits.push({ id: ent.id, entry: entryToDict(ent), score });
    }

    hits.sort((a, b) => b.score - a.score);
    return hits.slice(0, topK);
  }

  async imprint(op: ImprintOp, entry: Json, opts: ImprintOptions = {}): Promise<ImprintReceipt> {
    const t0 = Date.now();
    const mergeKey = opts.mergeKey ?? null;
    const tookMs = (): number => Date.now() - t0;

    // Idempotency: replay returns the recorded receipt.
    if (opts.imprintId !== undefined) {
      const seen = this.imprintSeen.get(opts.imprintId);
      if (seen !== undefined) {
        const existing = this.entries.get(seen);
        return receipt(this.engramId, op, {
          id: seen,
          version: existing ? existing.version : null,
          tookMs: tookMs(),
        });
      }
    }

    let resultingId: string | null = null;
    let version: number | null = null;

    if (op === "add") {
      const ent = this.makeEntry(entry, mergeKey);
      if (this.entries.has(ent.id)) {
        return receipt(this.engramId, op, { error: `entry id '${ent.id}' already exists`, tookMs: tookMs() });
      }
      this.store(ent);
      resultingId = ent.id;
      version = ent.version;
    } else if (op === "append") {
      let ent = this.makeEntry(entry, mergeKey);
      while (this.entries.has(ent.id)) {
        ent = this.makeEntry({ ...entry, id: newEngramId() }, mergeKey);
      }
      this.store(ent);
      resultingId = ent.id;
      version = ent.version;
    } else if (op === "upsert") {
      const existingIds = this.byMergeKey.get(mergeKey ?? "") ?? [];
      const targetId = existingIds[existingIds.length - 1];
      const old = targetId !== undefined ? this.entries.get(targetId) : undefined;
      if (old !== undefined) {
        const next = this.makeEntry({ ...entry, id: old.id }, mergeKey);
        next.createdAt = old.createdAt;
        next.version = old.version + 1;
        this.store(next, true);
        resultingId = next.id;
        version = next.version;
      } else {
        const ent = this.makeEntry(entry, mergeKey);
        this.store(ent);
        resultingId = ent.id;
        version = ent.version;
      }
    } else if (op === "merge") {
      const existingIds = this.byMergeKey.get(mergeKey ?? "") ?? [];
      const targetId = existingIds[existingIds.length - 1];
      const old = targetId !== undefined ? this.entries.get(targetId) : undefined;
      if (old === undefined) {
        return receipt(this.engramId, op, { error: `no entry for merge_key='${mergeKey}'`, tookMs: tookMs() });
      }
      const now = new Date().toISOString();
      const next: MemEntry = {
        id: old.id,
        content: deepMerge(old.content, (entry as Json)["content"]),
        tags: [...new Set([...old.tags, ...asStringArray((entry as Json)["tags"])])],
        mergeKey: old.mergeKey,
        version: old.version + 1,
        createdAt: old.createdAt,
        updatedAt: now,
        extra: asObject(deepMerge(old.extra, (entry as Json)["meta"])),
      };
      this.store(next, true);
      resultingId = next.id;
      version = next.version;
    } else if (op === "delete") {
      let targetId: string | null = null;
      const entId = (entry as Json)["id"];
      if (typeof entId === "string") {
        targetId = entId;
      } else if (mergeKey !== null) {
        const ids = this.byMergeKey.get(mergeKey) ?? [];
        targetId = ids[ids.length - 1] ?? null;
      }
      if (targetId === null || !this.entries.has(targetId)) {
        return receipt(this.engramId, op, { tookMs: tookMs() });
      }
      this.evict(targetId);
      resultingId = targetId;
      version = null;
    } else {
      return receipt(this.engramId, op, { error: `unknown op '${op as string}'`, tookMs: tookMs() });
    }

    if (opts.imprintId !== undefined && resultingId !== null) {
      this.imprintSeen.set(opts.imprintId, resultingId);
    }
    return receipt(this.engramId, op, { id: resultingId, version, tookMs: tookMs() });
  }

  /** Test/debug helper - NOT part of the Engram contract. */
  snapshot(): Json[] {
    return [...this.entries.values()].map(entryToDict);
  }

  private makeEntry(entry: Json, mergeKey: string | null): MemEntry {
    const id = typeof entry["id"] === "string" ? (entry["id"] as string) : newEngramId();
    const now = new Date().toISOString();
    return {
      id,
      content: entry["content"],
      tags: asStringArray(entry["tags"]),
      mergeKey,
      version: 1,
      createdAt: now,
      updatedAt: now,
      extra: asObject(entry["meta"]),
    };
  }

  private store(ent: MemEntry, replace = false): void {
    if (replace) {
      const old = this.entries.get(ent.id);
      if (old !== undefined && old.mergeKey) {
        const bucket = this.byMergeKey.get(old.mergeKey);
        if (bucket) {
          const idx = bucket.indexOf(ent.id);
          if (idx >= 0) bucket.splice(idx, 1);
          if (bucket.length === 0) this.byMergeKey.delete(old.mergeKey);
        }
      }
    }
    this.entries.set(ent.id, ent);
    if (ent.mergeKey) {
      const bucket = this.byMergeKey.get(ent.mergeKey);
      if (bucket) bucket.push(ent.id);
      else this.byMergeKey.set(ent.mergeKey, [ent.id]);
    }
  }

  private evict(entryId: string): void {
    const ent = this.entries.get(entryId);
    this.entries.delete(entryId);
    if (ent === undefined) return;
    if (ent.mergeKey) {
      const bucket = this.byMergeKey.get(ent.mergeKey);
      if (bucket) {
        const idx = bucket.indexOf(entryId);
        if (idx >= 0) bucket.splice(idx, 1);
        if (bucket.length === 0) this.byMergeKey.delete(ent.mergeKey);
      }
    }
  }
}

/** Conservative deep merge: dicts merge, lists concat-dedup, scalars overwrite. */
export function deepMerge(base: unknown, incoming: unknown): unknown {
  if (incoming === undefined || incoming === null) return base;
  const bothObjects =
    base !== null && typeof base === "object" && !Array.isArray(base) &&
    typeof incoming === "object" && !Array.isArray(incoming);
  if (bothObjects) {
    const out: Json = { ...(base as Json) };
    for (const [k, v] of Object.entries(incoming as Json)) {
      out[k] = k in out ? deepMerge(out[k], v) : v;
    }
    return out;
  }
  if (Array.isArray(base) && Array.isArray(incoming)) {
    const seen = new Set<string>();
    const out: unknown[] = [];
    for (const item of [...base, ...incoming]) {
      const key = JSON.stringify(item);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(item);
    }
    return out;
  }
  return incoming;
}
