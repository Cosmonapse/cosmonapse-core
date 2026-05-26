/**
 * @cosmonapse/sdk — registry store
 *
 * The one mandatory persistent surface: a live view of the Neurons a namespace
 * has seen. Ported from `cosmonapse.storage.base` / `cosmonapse.storage.memory`.
 *
 * Timestamps are RFC 3339 strings (matching the envelope's `ts`), so records
 * serialise cleanly to JSON. A backend is conformant iff it behaves like
 * MemoryRegistryStore (the reference implementation).
 *
 * (Python additionally ships sqlite/postgres backends; only the in-memory
 * backend is ported here. Implement the RegistryStore interface for others.)
 */

export type NeuronStatus = "registered" | "draining" | "deregistered";

/** A live view of one Neuron the namespace has seen. */
export interface NeuronRecord {
  neuron_id: string;
  capabilities: string[];
  version: string | null;
  status: NeuronStatus;
  last_heartbeat: string | null;
  registered_at: string;
}

export interface NeuronRecordInit {
  neuron_id: string;
  capabilities?: string[];
  version?: string | null;
  status?: NeuronStatus;
  last_heartbeat?: string | null;
  registered_at?: string;
}

/** Build a NeuronRecord, filling defaults (status "registered", now()). */
export function neuronRecord(init: NeuronRecordInit): NeuronRecord {
  return {
    neuron_id: init.neuron_id,
    capabilities: init.capabilities ?? [],
    version: init.version ?? null,
    status: init.status ?? "registered",
    last_heartbeat: init.last_heartbeat ?? null,
    registered_at: init.registered_at ?? new Date().toISOString(),
  };
}

export interface ListOptions {
  capability?: string;
  includeDeregistered?: boolean;
}

/**
 * Abstract registry store. All methods are async. Implementations must be safe
 * for concurrent calls within one event loop; cross-process consistency is a
 * backend concern (use a shared DB when multiple Cortices share state).
 */
export interface RegistryStore {
  connect(): Promise<void>;
  close(): Promise<void>;
  /** Insert or update a record by neuron_id (called on REGISTER). */
  upsert(record: NeuronRecord): Promise<void>;
  /** Mark an existing record deregistered. No-op if unknown. */
  markDeregistered(neuronId: string): Promise<void>;
  /**
   * Update last_heartbeat (and optionally status). If unknown, the backend
   * MAY create a thin record — tolerating heartbeats that arrive before
   * REGISTER.
   */
  touchHeartbeat(neuronId: string, ts: string, status?: NeuronStatus): Promise<void>;
  get(neuronId: string): Promise<NeuronRecord | null>;
  list(opts?: ListOptions): Promise<NeuronRecord[]>;
}

/** In-process RegistryStore. Default backend; reset on process restart. */
export class MemoryRegistryStore implements RegistryStore {
  private records = new Map<string, NeuronRecord>();

  async connect(): Promise<void> {
    /* nothing to open */
  }

  async close(): Promise<void> {
    this.records.clear();
  }

  async upsert(record: NeuronRecord): Promise<void> {
    // Preserve the original registered_at if we've seen this neuron before.
    const existing = this.records.get(record.neuron_id);
    const merged: NeuronRecord = existing
      ? { ...record, registered_at: existing.registered_at }
      : { ...record };
    this.records.set(record.neuron_id, merged);
  }

  async markDeregistered(neuronId: string): Promise<void> {
    const rec = this.records.get(neuronId);
    if (rec) rec.status = "deregistered";
  }

  async touchHeartbeat(neuronId: string, ts: string, status?: NeuronStatus): Promise<void> {
    const rec = this.records.get(neuronId);
    if (!rec) {
      this.records.set(
        neuronId,
        neuronRecord({
          neuron_id: neuronId,
          last_heartbeat: ts,
          ...(status ? { status } : {}),
        }),
      );
      return;
    }
    rec.last_heartbeat = ts;
    if (status) rec.status = status;
  }

  async get(neuronId: string): Promise<NeuronRecord | null> {
    return this.records.get(neuronId) ?? null;
  }

  async list(opts: ListOptions = {}): Promise<NeuronRecord[]> {
    const out: NeuronRecord[] = [];
    for (const rec of this.records.values()) {
      if (!opts.includeDeregistered && rec.status === "deregistered") continue;
      if (opts.capability !== undefined && !rec.capabilities.includes(opts.capability)) continue;
      out.push(rec);
    }
    return out;
  }
}
