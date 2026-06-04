/**
 * @cosmonapse/sdk — Postgres registry store
 *
 * Ported from `cosmonapse.storage.postgres`. Backed by `pg` (node-postgres),
 * lazy-imported as an optional dependency: `npm i pg`. Only `connect()` raises
 * a clear error when the package is missing.
 *
 * The schema is bootstrapped on first `connect()`. Use a dedicated schema /
 * database for the Cortex if you don't want it sharing a namespace with your
 * application tables.
 *
 * Records round-trip the same shape as the Python SDK: capabilities as JSONB,
 * timestamps as RFC 3339 strings on the wire.
 */

import type {
  ListOptions,
  NeuronRecord,
  NeuronStatus,
  RegistryStore,
} from "./storage.js";

const SCHEMA = `
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
`;

// Minimal structural types for the slice of node-postgres we use.
interface PgQueryResult<R> {
  rows: R[];
}
interface PgPool {
  query<R = unknown>(text: string, params?: unknown[]): Promise<PgQueryResult<R>>;
  end(): Promise<void>;
}
type PgModule = {
  Pool: new (config: { connectionString: string; min?: number; max?: number }) => PgPool;
};

interface NeuronRow {
  neuron_id: string;
  capabilities: unknown;
  version: string | null;
  status: string;
  last_heartbeat: Date | string | null;
  registered_at: Date | string | null;
}

function toIso(value: Date | string | null): string | null {
  if (value === null) return null;
  return value instanceof Date ? value.toISOString() : value;
}

function recordFromRow(row: NeuronRow): NeuronRecord {
  let caps = row.capabilities;
  if (typeof caps === "string") caps = JSON.parse(caps) as unknown;
  return {
    neuron_id: row.neuron_id,
    capabilities: Array.isArray(caps) ? (caps as string[]) : [],
    version: row.version ?? null,
    status: (row.status as NeuronStatus) ?? "registered",
    last_heartbeat: toIso(row.last_heartbeat),
    registered_at: toIso(row.registered_at) ?? new Date().toISOString(),
  };
}

export interface PostgresRegistryStoreOptions {
  /** Postgres DSN, e.g. "postgresql://user:pass@host:5432/db". */
  dsn: string;
  minSize?: number;
  maxSize?: number;
}

/** Postgres-backed RegistryStore via node-postgres connection pool. */
export class PostgresRegistryStore implements RegistryStore {
  private readonly dsn: string;
  private readonly minSize: number;
  private readonly maxSize: number;
  private pool: PgPool | null = null;

  constructor(opts: PostgresRegistryStoreOptions) {
    this.dsn = opts.dsn;
    this.minSize = opts.minSize ?? 1;
    this.maxSize = opts.maxSize ?? 5;
  }

  async connect(): Promise<void> {
    if (this.pool !== null) return;
    let mod: PgModule;
    // Indirect the specifier so tsc does not statically resolve the optional
    // driver's types (it ships none) during the .d.ts build. Stays a lazy,
    // external runtime import.
    const specifier = "pg";
    try {
      mod = (await import(specifier)) as unknown as PgModule;
    } catch (err) {
      throw new Error(
        "PostgresRegistryStore requires the 'pg' package. Install it with: npm i pg" +
          (err instanceof Error ? ` (${err.message})` : ""),
      );
    }
    this.pool = new mod.Pool({
      connectionString: this.dsn,
      min: this.minSize,
      max: this.maxSize,
    });
    await this.pool.query(SCHEMA);
  }

  async close(): Promise<void> {
    if (this.pool === null) return;
    await this.pool.end();
    this.pool = null;
  }

  private require(): PgPool {
    if (this.pool === null) throw new Error("PostgresRegistryStore.connect() not called");
    return this.pool;
  }

  async upsert(record: NeuronRecord): Promise<void> {
    await this.require().query(
      `INSERT INTO cosmonapse_neurons
         (neuron_id, capabilities, version, status, last_heartbeat, registered_at)
       VALUES ($1, $2::jsonb, $3, $4, $5, $6)
       ON CONFLICT (neuron_id) DO UPDATE SET
         capabilities   = EXCLUDED.capabilities,
         version        = EXCLUDED.version,
         status         = EXCLUDED.status,
         last_heartbeat = EXCLUDED.last_heartbeat`,
      [
        record.neuron_id,
        JSON.stringify(record.capabilities),
        record.version,
        record.status,
        record.last_heartbeat,
        record.registered_at,
      ],
    );
  }

  async markDeregistered(neuronId: string): Promise<void> {
    await this.require().query(
      "UPDATE cosmonapse_neurons SET status = 'deregistered' WHERE neuron_id = $1",
      [neuronId],
    );
  }

  async touchHeartbeat(neuronId: string, ts: string, status?: NeuronStatus): Promise<void> {
    const pool = this.require();
    if (status !== undefined) {
      await pool.query(
        `INSERT INTO cosmonapse_neurons (neuron_id, last_heartbeat, status)
         VALUES ($1, $2, $3)
         ON CONFLICT (neuron_id) DO UPDATE SET
           last_heartbeat = EXCLUDED.last_heartbeat,
           status         = EXCLUDED.status`,
        [neuronId, ts, status],
      );
    } else {
      await pool.query(
        `INSERT INTO cosmonapse_neurons (neuron_id, last_heartbeat)
         VALUES ($1, $2)
         ON CONFLICT (neuron_id) DO UPDATE SET
           last_heartbeat = EXCLUDED.last_heartbeat`,
        [neuronId, ts],
      );
    }
  }

  async get(neuronId: string): Promise<NeuronRecord | null> {
    const res = await this.require().query<NeuronRow>(
      "SELECT neuron_id, capabilities, version, status, last_heartbeat, registered_at " +
        "FROM cosmonapse_neurons WHERE neuron_id = $1",
      [neuronId],
    );
    const row = res.rows[0];
    return row ? recordFromRow(row) : null;
  }

  async list(opts: ListOptions = {}): Promise<NeuronRecord[]> {
    const clauses: string[] = [];
    const params: unknown[] = [];
    if (!opts.includeDeregistered) clauses.push("status <> 'deregistered'");
    if (opts.capability !== undefined) {
      params.push(opts.capability);
      clauses.push(`capabilities @> to_jsonb($${params.length}::text)`);
    }
    let sql =
      "SELECT neuron_id, capabilities, version, status, last_heartbeat, registered_at " +
      "FROM cosmonapse_neurons";
    if (clauses.length) sql += " WHERE " + clauses.join(" AND ");
    const res = await this.require().query<NeuronRow>(sql, params);
    return res.rows.map(recordFromRow);
  }
}
