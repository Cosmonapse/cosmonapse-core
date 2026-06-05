/**
 * @cosmonapse/sdk  -  envelope
 *
 * Signal envelope types and codec. The TypeScript surface mirrors the Python
 * `cosmonapse.envelope` module 1:1 (see ENVELOPE_SPEC.md §7). Every message
 * crossing the Synapse is a Signal  -  a JSON object conforming to this schema.
 *
 * Producer tags (who emits each type):
 *   [A]  Axon (skill/connector)
 *   [C]  Cortex (developer-built orchestrating component)
 */

import { ulid } from "ulid";

// ---------------------------------------------------------------------------
// ULID helpers
// ---------------------------------------------------------------------------

/** Return a prefixed event ULID: `evt_<26-char ULID>`. */
export function newEventId(): string {
  return `evt_${ulid()}`;
}

/** Return a prefixed trace ULID: `trc_<26-char ULID>`. */
export function newTraceId(): string {
  return `trc_${ulid()}`;
}

function nowUtc(): string {
  // RFC 3339 UTC timestamp with millisecond precision.
  return new Date().toISOString();
}

// ---------------------------------------------------------------------------
// Signal types
// ---------------------------------------------------------------------------

export const SignalType = {
  // Lifecycle [A] / [C]
  TASK: "TASK",
  AGENT_OUTPUT: "AGENT_OUTPUT",
  FINAL: "FINAL",
  ERROR: "ERROR",

  // Routing [C]
  TASK_OFFER: "TASK_OFFER",
  BID: "BID",
  TASK_AWARDED: "TASK_AWARDED",
  TASK_DECLINED: "TASK_DECLINED",

  // Cognition [C]
  THOUGHT_DELTA: "THOUGHT_DELTA",
  PLAN: "PLAN",
  TOOL_CALL: "TOOL_CALL",
  TOOL_RESULT: "TOOL_RESULT",

  // Memory [C]
  MEMORY_APPEND: "MEMORY_APPEND",
  ESCALATION: "ESCALATION",

  // Coordination [C] / [A]
  CONSENSUS: "CONSENSUS",
  CONTEXT_SYNC: "CONTEXT_SYNC",
  CRITIQUE: "CRITIQUE",
  CLARIFICATION: "CLARIFICATION",

  // Agent management [A]
  REGISTER: "REGISTER",
  DEREGISTER: "DEREGISTER",
  HEARTBEAT: "HEARTBEAT",
} as const;

export type SignalType = (typeof SignalType)[keyof typeof SignalType];

/** Types the Axon (skill) is allowed to produce. */
export const AXON_TYPES: ReadonlySet<SignalType> = new Set<SignalType>([
  SignalType.AGENT_OUTPUT,
  SignalType.CLARIFICATION,
  SignalType.ERROR,
  SignalType.REGISTER,
  SignalType.DEREGISTER,
  SignalType.HEARTBEAT,
]);

/** Types the Cortex (orchestrator) is allowed to produce. */
export const SYNAPSE_TYPES: ReadonlySet<SignalType> = new Set<SignalType>([
  SignalType.TASK,
  SignalType.FINAL,
  SignalType.ERROR,
  SignalType.TASK_OFFER,
  SignalType.BID,
  SignalType.TASK_AWARDED,
  SignalType.TASK_DECLINED,
  SignalType.THOUGHT_DELTA,
  SignalType.PLAN,
  SignalType.TOOL_CALL,
  SignalType.TOOL_RESULT,
  SignalType.MEMORY_APPEND,
  SignalType.ESCALATION,
  SignalType.CONSENSUS,
  SignalType.CONTEXT_SYNC,
  SignalType.CRITIQUE,
]);

// ---------------------------------------------------------------------------
// Base envelope
// ---------------------------------------------------------------------------

export type Json = Record<string, unknown>;

/**
 * The universal envelope for every message crossing the Synapse.
 *
 * - `v`         Protocol version. Always `"1"` for this release.
 * - `id`        Unique event ID. Format: `evt_<26-char ULID>`.
 * - `trace_id`  Groups all Signals belonging to one logical workflow (`trc_<ULID>`).
 * - `parent_id` The id of the Signal that caused this one. Optional.
 * - `type`      One of the {@link SignalType} values.
 * - `neuron`    Identifier of the producing Neuron. Required for Axon types.
 * - `ts`        RFC 3339 UTC timestamp of emission.
 * - `payload`   Type-specific content.
 * - `meta`      Non-semantic annotations: model name, token counts, cost, etc.
 */
export interface Signal {
  v: string;
  id: string;
  trace_id: string;
  parent_id: string | null;
  type: SignalType;
  neuron: string | null;
  ts: string;
  payload: Json;
  meta: Json;
}

export interface NewSignalInput {
  type: SignalType;
  trace_id?: string;
  parent_id?: string | null;
  neuron?: string | null;
  payload?: Json;
  meta?: Json;
  v?: string;
  id?: string;
  ts?: string;
}

/** Construct a fully-populated, validated Signal, filling protocol defaults. */
export function createSignal(input: NewSignalInput): Signal {
  const signal: Signal = {
    v: input.v ?? "1",
    id: input.id ?? newEventId(),
    trace_id: input.trace_id ?? newTraceId(),
    parent_id: input.parent_id ?? null,
    type: input.type,
    neuron: input.neuron ?? null,
    ts: input.ts ?? nowUtc(),
    payload: input.payload ?? {},
    meta: input.meta ?? {},
  };
  validateSignal(signal);
  return signal;
}

/** Throw if the envelope's identifier fields violate the spec. */
export function validateSignal(signal: Signal): void {
  if (!signal.id.startsWith("evt_")) {
    throw new Error(`Signal id must start with 'evt_', got: ${signal.id}`);
  }
  if (!signal.trace_id.startsWith("trc_")) {
    throw new Error(`trace_id must start with 'trc_', got: ${signal.trace_id}`);
  }
  if (signal.parent_id !== null && !signal.parent_id.startsWith("evt_")) {
    throw new Error(`parent_id must start with 'evt_', got: ${signal.parent_id}`);
  }
}

/** Serialise to UTF-8 JSON bytes for wire transmission. */
export function encode(signal: Signal): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(signal));
}

/** Deserialise from JSON bytes or string into a validated Signal. */
export function decode(data: Uint8Array | string): Signal {
  const text = typeof data === "string" ? data : new TextDecoder().decode(data);
  const parsed = JSON.parse(text) as Partial<Signal>;
  if (parsed.type === undefined) {
    throw new Error("Signal is missing required field 'type'");
  }
  return createSignal(parsed as NewSignalInput);
}

/**
 * Construct a reply Signal that shares `source`'s trace_id and sets
 * `parent_id` to `source`'s id.
 */
export function reply(
  source: Signal,
  opts: {
    type: SignalType;
    payload?: Json;
    neuron?: string | null;
    meta?: Json;
  },
): Signal {
  return createSignal({
    type: opts.type,
    trace_id: source.trace_id,
    parent_id: source.id,
    payload: opts.payload ?? {},
    neuron: opts.neuron ?? source.neuron,
    meta: opts.meta ?? {},
  });
}
