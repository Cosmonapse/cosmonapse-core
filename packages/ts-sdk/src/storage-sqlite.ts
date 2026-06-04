/**
 * @cosmonapse/sdk — SQLite registry store
 *
 * Ported from `cosmonapse.storage.sqlite`. A single file on disk (or
 * `:memory:`), backed by `better-sqlite3` (lazy-imported, optional dependency:
 * `npm i better-sqlite3`). The Python port uses stdlib sqlite3; Node has no
 * stable built-in equivalent, so we use the de-facto standard driver.
 *
 * The schema is created on `connect()` if it does not already exist. Records
 * round-trip the same on-disk shape as the Python SDK: capabilities as a JSON
 * array, timestamps as RFC 3339 strings.
 */

import type {
  ListOptions,
  NeuronRecord,
  NeuronStatus,
  RegistryStore,
} from "./storage.js";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS neurons (
    neuron_id      TEXT PRIMARY KEY,
    capabilities   TEXT NOT NULL DEFAULT '[]',
    version        TEXT,
    status         TEXT NOT NULL DEFAULT 'registered',
    last_heartbeat TEXT,
    registered_at  TEXT NOT NULL
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
type SqliteModule = {
  default: new (path: string) => SqliteDatabase;
};

interface NeuronRow {
  neuron_id: string;
  capabilities: string | null;
  version: string | null;
  status: string;
  last_heartbeat: string | null;
  registered_at: string | null;
}

function recordFromRow(row: NeuronRow): NeuronRecord {
  return {
    neuron_id: row.neuron_id,
    capabilities: row.capabilities ? (JSON.parse(row.capabilities) as string[]) : [],
    version: row.version ?? null,
    status: (row.status as NeuronStatus) ?? "registered",
    last_heartbeat: row.last_heartbeat ?? null,
    registered_at: row.registered_at ?? new Date().toISOString(),
  };
}

/**
 * SQLite-backed RegistryStore.
 *
 * @param path Filesystem path to the DB file. Use `:memory:` for an ephemeral
 *   in-process DB (useful for tests; not shared across connections).
 */
export class SqliteRegistryStore implements RegistryStore {
  private db: SqliteDatabase | null = null;

  constructor(private readonly path: string = ":memory:") {}

  async connect(): Promise<void> {
    if (this.db !== null) return;
    let mod: SqliteModule;
    // Indirect the specifier so tsc does not statically resolve the optional
    // driver's types (it ships none) during the .d.ts build. Stays a lazy,
    // external runtime import.
    const specifier = "better-sqlite3";
    try {
      mod = (await import(specifier)) as unknown as SqliteModule;
    } catch (err) {
      throw new Error(
        "SqliteRegistryStore requires the 'better-sqlite3' package. " +
          "Install it with: npm i better-sqlite3" +
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
    if (this.db === null) throw new Error("SqliteRegistryStore.connect() not called");
    return this.db;
  }

  async upsert(record: NeuronRecord): Promise<void> {
    const db = this.require();
    const existing = db
      .prepare("SELECT registered_at FROM neurons WHERE neuron_id = ?")
      .get(record.neuron_id) as { registered_at: string } | undefined;
    const registeredAt = existing ? existing.registered_at : record.registered_at;
    db.prepare(
      `INSERT INTO neurons
         (neuron_id, capabilities, version, status, last_heartbeat, registered_at)
       VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(neuron_id) DO UPDATE SET
         capabilities   = excluded.capabilities,
         version        = excluded.version,
         status         = excluded.status,
         last_heartbeat = excluded.last_heartbeat`,
    ).run(
      record.neuron_id,
      JSON.stringify(record.capabilities),
      record.version,
      record.status,
      record.last_heartbeat,
      registeredAt,
    );
  }

  async markDeregistered(neuronId: string): Promise<void> {
    this.require()
      .prepare("UPDATE neurons SET status = 'deregistered' WHERE neuron_id = ?")
      .run(neuronId);
  }

  async touchHeartbeat(neuronId: string, ts: string, status?: NeuronStatus): Promise<void> {
    const db = this.require();
    const existing = db
      .prepare("SELECT neuron_id FROM neurons WHERE neuron_id = ?")
      .get(neuronId) as { neuron_id: string } | undefined;
    if (!existing) {
      db.prepare(
        `INSERT INTO neurons
           (neuron_id, capabilities, version, status, last_heartbeat, registered_at)
         VALUES (?, '[]', NULL, ?, ?, ?)`,
      ).run(neuronId, status ?? "registered", ts, new Date().toISOString());
    } else if (status !== undefined) {
      db.prepare(
        "UPDATE neurons SET last_heartbeat = ?, status = ? WHERE neuron_id = ?",
      ).run(ts, status, neuronId);
    } else {
      db.prepare("UPDATE neurons SET last_heartbeat = ? WHERE neuron_id = ?").run(ts, neuronId);
    }
  }

  async get(neuronId: string): Promise<NeuronRecord | null> {
    const row = this.require()
      .prepare(
        "SELECT neuron_id, capabilities, version, status, last_heartbeat, registered_at " +
          "FROM neurons WHERE neuron_id = ?",
      )
      .get(neuronId) as NeuronRow | undefined;
    return row ? recordFromRow(row) : null;
  }

  async list(opts: ListOptions = {}): Promise<NeuronRecord[]> {
    let sql =
      "SELECT neuron_id, capabilities, version, status, last_heartbeat, registered_at FROM neurons";
    if (!opts.includeDeregistered) sql += " WHERE status != 'deregistered'";
    const rows = this.require().prepare(sql).all() as NeuronRow[];
    let out = rows.map(recordFromRow);
    if (opts.capability !== undefined) {
      out = out.filter((r) => r.capabilities.includes(opts.capability!));
    }
    return out;
  }
}
